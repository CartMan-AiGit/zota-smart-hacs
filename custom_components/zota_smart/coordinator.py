"""Координатор ZOTA Smart: опрос модуля и доставка состояния в HA.

Потоки: TLS-транспорт работает в отдельном демон-потоке и вызывает
колбэки ``on_client`` / ``on_frame`` / ``on_disconnect`` именно в нём.
Поэтому координатор никогда не трогает состояние HA напрямую из колбэка —
всё маршалится в event loop через ``hass.loop.call_soon_threadsafe``.

Протокол (§11.3, §11.7):

* ``link=2 op=2`` — регистрация модуля: payload ``<u32 subtype><u32 serial>``.
  Отвечать на неё **нельзя** (§11.3, правило 1) — просто запоминаем серийник.
* ``link=2 op=4`` — keepalive каждые 30 с, **ответа не требует**.
* ``link=1 op=1`` — ответ модуля: ``<u16 cmd_echo><u16 result><data>``.
  Для ``cmd_echo=0`` в ``data`` лежит info-блок (53 байта).

Запись (фаза 4) — через :meth:`ZotaCoordinator.send_write`: метод возвращает
``Future``, который резолвится по ACK с тем же ``cmd_echo``.

Режим PROXY (``entry.data["proxy"]``) — мост к настоящему облаку ZOTA
(:class:`.proxy.ZotaCloudBridge`): транспорт отдаёт мосту сырые байты
модуля, мост прозрачно пересылает их в облако и обратно, а разбор кадров
(обоих направлений) делает сам мост. Поэтому в PROXY-режиме транспортный
``on_frame`` отключён — иначе один кадр разбирался бы дважды. Мост
поднимается лениво, при первом подключении модуля (:meth:`_on_client`).

Координатор кэширует сырой кадр регистрации модуля (``link=2 op=2``),
перехваченный в направлении модуль → облако
(:meth:`_on_registration_frame`). Модуль шлёт регистрацию сразу при своём
подключении, и если канал к облаку в этот момент ещё не поднят, кадр
уходит в никуда: облако не видит серийник и рвёт «молчаливую» сессию через
таймаут. Кэш позволяет мосту отправить регистрацию первым кадром новой
сессии с облаком (:meth:`_get_registration_frame`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import struct
import threading
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CMD_INFO,
    CONF_CLOUD_ADDRESS,
    CONF_FRAME_LOG,
    CONF_NAME,
    CONF_PORT,
    CONF_POWER_KW,
    CONF_PROXY,
    CONF_PSK,
    CONF_TEMP_SENSOR,
    DEFAULT_CLOUD_ADDRESS,
    DEFAULT_FRAME_LOG,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_POWER_KW,
    DEFAULT_PSK,
    DEFAULT_TEMP_SENSOR,
    DEVICE_IDENTIFIER,
    DOMAIN,
    HEADER_SIZE,
    LINK_FLAT,
    LINK_REGISTER,
    OPCODE_ACK,
    OPCODE_COMMAND,
    OPCODE_KEEPALIVE,
    OPCODE_REGISTER,
    POLL_INTERVAL,
    PSK_IDENTITY,
    RESULT_NAK,
    CMD_VALVE,
    VALVE_CONST_FLAG,
    VALVE_CONST_TEMP2,
    VALVE_FULL_OPEN_MAX,
    VALVE_FULL_OPEN_MIN,
    VALVE_MODE_FLOOR,
    VALVE_MODE_HEATING,
    VALVE_MODE_OFF,
    VALVE_ON_TIME_MAX,
    VALVE_ON_TIME_MIN,
    VALVE_PERIOD_MAX,
    VALVE_PERIOD_MIN,
    VALVE_TARGET_FLOOR_MAX,
    VALVE_TARGET_MAX,
    VALVE_TARGET_MIN,
)
from .protocol import build_read, build_write, parse_ack, set_frame_logging
from .proxy import ZotaCloudBridge
from .state import ZotaState, state_from_info
from .tls_server import TLSServerTransport

_LOGGER = logging.getLogger(__name__)

type ZotaConfigEntry = ConfigEntry

#: Таймаут ожидания ACK на запись, с.
WRITE_TIMEOUT = 5.0

#: Диапазоны полей клапана для валидации (поле → (min, max)).
_VALVE_RANGES: dict[str, tuple[int, int]] = {
    "mode": (VALVE_MODE_OFF, VALVE_MODE_HEATING),
    "target": (VALVE_TARGET_MIN, VALVE_TARGET_MAX),
    "on_time": (VALVE_ON_TIME_MIN, VALVE_ON_TIME_MAX),
    "period": (VALVE_PERIOD_MIN, VALVE_PERIOD_MAX),
    "full_open": (VALVE_FULL_OPEN_MIN, VALVE_FULL_OPEN_MAX),
}


def build_valve_payload(
    mode: int, target: int, on_time: int, period: int, full_open: int
) -> bytes:
    """Собрать 11-байтовый payload записи клапана cmd=0x0C (§11.13.1).

    Формат: ``<u16 0x0C><u8 режим><u8 0x01><u8 целевая><u8 25><u8 включение>
    <u16 периодичность LE><u16 полное_открытие LE>``. Каждая запись — полный
    блок настроек, константы ``01`` и ``0x19`` фиксированы захватом 2026-11-06.
    """
    return struct.pack(
        "<HBBBBBHH",
        CMD_VALVE,
        mode,
        VALVE_CONST_FLAG,
        target,
        VALVE_CONST_TEMP2,
        on_time,
        period,
        full_open,
    )


class ZotaCoordinator(DataUpdateCoordinator[ZotaState]):
    """Держит TLS-транспорт, опрашивает модуль и хранит последнее состояние.

    Наследует ``DataUpdateCoordinator``, но ``_async_update_data`` не
    используется: опрос идёт по событию (модуль сам шлёт keepalive и ждёт
    команд), а состояние приходит из ``link=1 op=1``-ответа и доставляется
    через ``async_set_updated_data``. Наследование даёт готовую механику
    подписки сущностей (``async_add_listener``), ``last_update_success`` и
    ``available``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ZotaConfigEntry,
        transport: TLSServerTransport | None = None,
    ) -> None:
        """Создать координатор (транспорт можно передать снаружи — для тестов)."""
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.entry = entry
        conf = {**entry.data, **entry.options}
        self.name: str = conf.get(CONF_NAME, DEFAULT_NAME)
        self.port: int = conf.get(CONF_PORT, DEFAULT_PORT)
        #: Режим PROXY: мост к настоящему облаку ZOTA вместо прямой работы
        #: с модулем.
        self.proxy: bool = bool(conf.get(CONF_PROXY, False))
        #: Адрес облака ``host:port``.
        self.cloud_address: str = conf.get(CONF_CLOUD_ADDRESS, DEFAULT_CLOUD_ADDRESS)
        #: TLS-PSK ключ (hex). Обязателен: если ключ не задан, запуск падает
        #: с ``ConfigEntryNotReady`` (см. :meth:`async_start`).
        self.psk_hex: str = conf.get(CONF_PSK) or DEFAULT_PSK
        #: Мощность котла, кВт — определяет пресеты климат-сущности.
        self.power_kw: int = conf.get(CONF_POWER_KW, DEFAULT_POWER_KW)
        #: Мощность одной ступени, кВт: 3 ступени (0…3). Для 15 кВт — 5,
        #: поведение прежних записей не меняется.
        self.kw_per_stage: int = max(1, self.power_kw // 3)
        #: Запись кадров в /config/zota_frames.log (отладка, default False).
        self.frame_log: bool = bool(conf.get(CONF_FRAME_LOG, DEFAULT_FRAME_LOG))
        set_frame_logging(self.frame_log)
        #: Источник текущей температуры climate-сущности (supply/return/room/
        #: outside). Дефолт supply — поведение существующих записей не меняется.
        self.temp_sensor: str = conf.get(CONF_TEMP_SENSOR, DEFAULT_TEMP_SENSOR)

        self._transport = transport
        self._bridge: ZotaCloudBridge | None = None
        self._serial: int | None = None
        self._connected = False
        self._pending: dict[int, concurrent.futures.Future[int]] = {}
        #: Доступ к ``_pending`` из двух потоков (event loop регистрирует
        #: ожидание, поток транспорта резолвит по ACK) — под локом.
        self._pending_lock = threading.Lock()
        #: Сериализация записей «отправка + ожидание ACK»: без неё вторая
        #: быстрая команда с тем же cmd перезаписывает pending future первой
        #: и её await висит до таймаута. Future создаётся и таймаут
        #: планируется под локом — таймаут второй команды стартует с её
        #: отправки, а не копится с первой.
        self._write_serial = asyncio.Lock()
        self._poll_timer: asyncio.TimerHandle | None = None
        self._unsub_writes: list[Callable[[], None]] = []
        #: Сырые байты последнего кадра регистрации модуля (link=2 op=2).
        #: Кэш нужен, чтобы отправить регистрацию первым кадром новой
        #: сессии с облаком: модуль шлёт её сразу при своём подключении,
        #: и если канал к облаку ещё не поднят, кадр уходит в никуда —
        #: облако не видит серийник и рвёт «молчаливую» сессию по таймауту.
        self._registration_frame: bytes | None = None
        self._registration_lock = threading.Lock()

    # --- Публичный API ----------------------------------------------------

    @property
    def serial(self) -> int | None:
        """Серийник модуля (известен после регистрации)."""
        return self._serial

    @property
    def connected(self) -> bool:
        """Есть ли сейчас связь с модулем."""
        return self._connected

    @property
    def device_identifier(self) -> str:
        """Идентификатор устройства — постоянная константа.

        Серийник в идентичность **не** входит: он приходит уже после создания
        сущностей, и смена идентификатора заставила бы HA создать второй
        набор сущностей (старые остались бы со своим прежним идентификатором).
        Серийник доступен как ``serial_number`` в ``device_info``.
        """
        return DEVICE_IDENTIFIER

    async def async_start(self) -> None:
        """Поднять транспорт и запустить таймер опроса."""
        if not self.psk_hex:
            # PSK обязателен: без него TLS-хендшейк с модулем невозможен.
            # Это не временная ошибка, а незавершённая настройка — HA покажет
            # её в интерфейсе, и запись можно будет перенастроить.
            raise ConfigEntryNotReady(
                "Укажите PSK модуля в настройках интеграции"
            )
        if self._transport is None:
            self._transport = TLSServerTransport(
                host="0.0.0.0",
                port=self.port,
                psk_hex=self.psk_hex,
                psk_identity=PSK_IDENTITY,
                on_client=self._on_client,
                # В режиме PROXY разбор кадров делает мост: он кормит
                # кадрами и байты от модуля, и байты от облака, поэтому
                # транспортный разбор отключён, чтобы не было двойного
                # разбора одного и того же кадра.
                on_frame=None if self.proxy else self._on_frame,
                on_disconnect=self._on_disconnect,
                on_bytes=self._on_module_bytes if self.proxy else None,
                logger=_LOGGER,
            )
        if self.proxy:
            # Мост создаётся сразу (сеть не трогается), а его поток
            # поднимается лениво — при первом подключении модуля. Иначе
            # первые байты от модуля (регистрация) пришли бы в никуда:
            # модуль шлёт их сразу после рукопожатия.
            self._bridge = self._make_bridge()
        self._transport.start()
        self._schedule_poll()

    async def async_stop(self) -> None:
        """Остановить транспорт, таймер и записать незавершённые запросы."""
        if self._poll_timer is not None:
            self._poll_timer.cancel()
            self._poll_timer = None
        for unsub in self._unsub_writes:
            unsub()
        self._unsub_writes.clear()
        await self.stop_bridge()
        if self._transport is not None:
            # transport.stop() джойнит поток сервера — блокирующий вызов,
            # в event loop его выполнять нельзя.
            await self.hass.async_add_executor_job(self._transport.stop)
        self._fail_pending()
        self._set_available(False)

    # --- Мост к облаку (режим PROXY) --------------------------------------

    async def start_bridge(self) -> ZotaCloudBridge | None:
        """Поднять мост к облаку ZOTA.

        Мост стартует лениво — при первом подключении модуля
        (:meth:`_on_client`): пока модуля нет, незачем держать сессию с
        облаком.

        Возвращает мост или ``None``, если режим PROXY выключен.
        """
        return self._ensure_bridge()

    async def stop_bridge(self) -> None:
        """Остановить мост к облаку ZOTA (если он был поднят)."""
        bridge = self._bridge
        self._bridge = None
        if bridge is None:
            return
        # bridge.stop() джойнит поток моста — блокирующий вызов, в event
        # loop его выполнять нельзя.
        await self.hass.async_add_executor_job(bridge.stop)
        _LOGGER.warning("Мост к облаку ZOTA остановлен")

    def _send_to_module(self, data: bytes) -> bool:
        """Переслать байты от облака в модуль (дословно).

        Вызывается из потока моста.
        """
        if self._transport is None:
            return False
        return self._transport.send_bytes(data)

    def _on_module_bytes(self, data: bytes) -> None:
        """Сырые байты от модуля: переслать в облако и разобрать локально.

        Вызывается из потока транспорта.
        """
        bridge = self._bridge
        if bridge is None:
            return
        bridge.feed_from_module(data)

    def send_write(self, cmd: int, val: int) -> concurrent.futures.Future[int]:
        """Отправить запись ``cmd=val`` и вернуть Future с результатом ACK.

        Future резолвится значением ``result`` из ACK-ответа модуля
        (0 = OK, 1 = OK пусто, 2 = NAK) либо исключением по таймауту
        (:data:`WRITE_TIMEOUT`) или при обрыве соединения.

        Вызывать из event loop; фактическая отправка идёт из потока
        транспорта, а разбор ответа — тоже оттуда, поэтому резолвится
        Future потокобезопасно.

        Предпочтительный путь для сущностей — :meth:`async_send_write`:
        он сериализует записи и не блокирует event loop.
        """
        payload = build_write(cmd, val)[HEADER_SIZE:]
        return self._send_and_wait(cmd, payload)

    async def async_send_write(self, cmd: int, val: int) -> int:
        """Сериализованная запись ``cmd=val`` с ожиданием ACK (event loop).

        Цикл «отправка + ожидание ACK» идёт под ``_write_serial``: вторая
        команда ждёт завершения первой, pending future не перезаписывается.
        Отправка выполняется в executor — ретраи записи не блокируют
        event loop. Возвращает ``result`` ACK или бросает исключение
        (таймаут/нет связи; NAK-обёртки делает вызывающая сущность).
        """
        payload = build_write(cmd, val)[HEADER_SIZE:]
        return await self._async_send_and_wait(cmd, payload)

    async def async_send_raw(self, payload: bytes, timeout: float = WRITE_TIMEOUT) -> int:
        """Сериализованная :meth:`send_raw` (см. :meth:`async_send_write`)."""
        payload = bytes(payload)
        if len(payload) < 2:
            raise ValueError(
                "payload слишком короткий: %d байт (нужно ≥ 2)" % len(payload)
            )
        cmd_echo = struct.unpack_from("<H", payload, 0)[0]
        return await self._async_send_and_wait(cmd_echo, payload, timeout)

    async def async_send_valve_config(self, **changes: int) -> int:
        """Сериализованная :meth:`send_valve_config` (см. :meth:`async_send_write`)."""
        payload = self._build_valve_payload(**changes)
        return await self._async_send_and_wait(CMD_VALVE, payload)

    async def _async_send_and_wait(
        self, cmd_echo: int, payload: bytes, timeout: float = WRITE_TIMEOUT
    ) -> int:
        """Отправить payload под локом сериализации и дождаться ACK.

        Future регистрируется до отправки (ACK не может прийти раньше
        отправки), сама отправка — в executor, таймаут планируется после
        успешной отправки — время ожидания каждой команды конечно и не
        копится с предыдущих (``_expire_write`` сверяет идентичность
        future, просроченный таймер чужой команды безвреден).
        """
        async with self._write_serial:
            future: concurrent.futures.Future[int] = concurrent.futures.Future()
            with self._pending_lock:
                self._pending[cmd_echo] = future
            if self._transport is None:
                with self._pending_lock:
                    self._pending.pop(cmd_echo, None)
                raise RuntimeError("транспорт не запущен")
            ok = await self.hass.async_add_executor_job(
                self._transport.send_frame, LINK_FLAT, OPCODE_COMMAND, payload
            )
            if not ok:
                with self._pending_lock:
                    self._pending.pop(cmd_echo, None)
                raise RuntimeError("модуль не подключён")
            self._hass_timer(
                timeout, self._expire_write, cmd_echo, future, timeout
            )
            return await asyncio.wrap_future(future)

    def send_raw(self, payload: bytes, timeout: float = WRITE_TIMEOUT) -> (
        concurrent.futures.Future[int]
    ):
        """Отправить произвольный payload команды и дождаться ACK.

        Нужен для записей, длина которых не 4 байта: обычный
        :meth:`send_write` собирает ``<u16 cmd><u16 val>``, а насос 2
        пишется 6-байтным payload ``<u16 cmd><u8 mode><0><0><0>``.

        ``cmd_echo`` ожидаемого ACK берётся из первых двух байт payload
        (``struct.unpack_from("<H", payload)[0]``) — то есть из кода
        команды. Future резолвится значением ``result`` из ACK-ответа
        модуля (0 = OK, 1 = OK пусто, 2 = NAK) либо исключением по
        таймауту или при обрыве соединения.

        Вызывать из event loop — как и :meth:`send_write`.
        """
        payload = bytes(payload)
        if len(payload) < 2:
            raise ValueError(
                "payload слишком короткий: %d байт (нужно ≥ 2)" % len(payload)
            )
        cmd_echo = struct.unpack_from("<H", payload, 0)[0]
        return self._send_and_wait(cmd_echo, payload, timeout)

    def _send_and_wait(
        self, cmd_echo: int, payload: bytes, timeout: float = WRITE_TIMEOUT
    ) -> concurrent.futures.Future[int]:
        """Отправить payload команды и зарегистрировать ожидание ACK.

        Общий механизм ожидания ACK для :meth:`send_write` и
        :meth:`send_raw`: Future кладётся в ``_pending`` под ``cmd_echo``,
        и по приходу ACK с тем же ``cmd_echo`` резолвится
        (см. :meth:`_handle_ack`).
        """
        future: concurrent.futures.Future[int] = concurrent.futures.Future()
        with self._pending_lock:
            self._pending[cmd_echo] = future
        if self._transport is None:
            with self._pending_lock:
                self._pending.pop(cmd_echo, None)
            future.set_exception(RuntimeError("транспорт не запущен"))
            return future
        ok = self._transport.send_frame(
            LINK_FLAT, OPCODE_COMMAND, payload
        )
        if not ok:
            with self._pending_lock:
                self._pending.pop(cmd_echo, None)
            future.set_exception(RuntimeError("модуль не подключён"))
            return future
        self._hass_timer(
            timeout, self._expire_write, cmd_echo, future, timeout
        )
        return future

    def _expire_write(
        self,
        cmd: int,
        future: concurrent.futures.Future[int],
        timeout: float = WRITE_TIMEOUT,
    ) -> None:
        """Таймаут ожидания ACK на запись (event loop)."""
        with self._pending_lock:
            if self._pending.get(cmd) is not future:
                return
            self._pending.pop(cmd, None)
        if not future.done():
            future.set_exception(
                TimeoutError(
                    "нет ответа на запись cmd=%d за %.1f c" % (cmd, timeout)
                )
            )

    def poll_now(self) -> bool:
        """Запросить info-блок немедленно, не дожидаясь планового опроса.

        Вызывается после успешной записи, чтобы как можно скорее увидеть
        новое состояние котла (обычный таймер опроса срабатывает только
        через :data:`POLL_INTERVAL` секунд).

        Возвращает ``True``, если запрос отправлен, ``False`` — если модуль
        не подключён (тогда ждать нечего).
        """
        if self._transport is None or not self._transport.is_connected:
            return False
        return self._transport.send_frame(
            LINK_FLAT, OPCODE_COMMAND, build_read(CMD_INFO)[HEADER_SIZE:]
        )

    async def async_poll_now(self) -> bool:
        """``poll_now`` без блокировки event loop: отправка в executor.

        Сущности после успешной записи вызывают именно его — ретраи записи
        не должны крутиться в event loop.
        """
        if self._transport is None or not self._transport.is_connected:
            return False
        return await self.hass.async_add_executor_job(
            self._transport.send_frame,
            LINK_FLAT,
            OPCODE_COMMAND,
            build_read(CMD_INFO)[HEADER_SIZE:],
        )

    def send_valve_config(self, **changes: int) -> concurrent.futures.Future[int]:
        """Записать настройки клапана (cmd=0x0C) и вернуть Future с result ACK.

        Семантика — см. :meth:`_build_valve_payload`. Сущностям предпочтителен
        сериализованный :meth:`async_send_valve_config`.
        """
        return self._send_and_wait(CMD_VALVE, self._build_valve_payload(**changes))

    def _build_valve_payload(self, **changes: int) -> bytes:
        """Собрать и провалидировать write-payload настроек клапана (§11.13).

        Каждая запись — полный блок настроек одним кадром (§11.13.1):
        текущие значения берутся из последнего info-состояния, поля из
        ``changes`` подменяются. Допустимые ключи: ``mode``, ``target``,
        ``on_time``, ``period``, ``full_open``; значения валидируются по
        диапазонам.

        Двойная целевая и защита Тёплого пола (§11.13.6): mode-байт задаёт
        контур И активирует его, отдельно менять целевую неактивного контура
        нельзя. Поэтому при смене ``mode`` целевая подставляется автоматически
        из состояния по контуру назначения (mode=1 → ``[10]``, иначе ``[7]``).
        При смене ``target`` без ``mode`` контур — текущий активный; если это
        Тёплый пол и target > 50 — ``ValueError`` до отправки (котёл вернул бы
        ACK result=1 без изменений).
        """
        state = self.data
        if state is None:
            raise RuntimeError("нет данных о состоянии котла — клапан не записать")
        unknown = set(changes) - set(_VALVE_RANGES)
        if unknown:
            raise ValueError("неизвестные поля клапана: %s" % sorted(unknown))
        fields: dict[str, int] = {
            "mode": state.valve_mode,
            "target": state.valve_target,
            "on_time": state.valve_on_time,
            "period": state.valve_period,
            "full_open": state.valve_full_open,
        }
        if "mode" in changes and "target" not in changes:
            # Маршрутизация целевой по контуру назначения (§11.13.6).
            if changes["mode"] == VALVE_MODE_FLOOR:
                fields["target"] = state.valve_target_floor
            else:
                fields["target"] = state.valve_target
        fields.update(changes)
        if (
            "mode" not in changes
            and fields["mode"] == VALVE_MODE_FLOOR
            and fields["target"] > VALVE_TARGET_FLOOR_MAX
        ):
            raise ValueError(
                "целевая Тёплого пола не может быть больше %d (получено %d)"
                % (VALVE_TARGET_FLOOR_MAX, fields["target"])
            )
        for name, (low, high) in _VALVE_RANGES.items():
            value = fields[name]
            if not low <= value <= high:
                raise ValueError(
                    "поле клапана %s=%d вне диапазона %d…%d" % (name, value, low, high)
                )
        payload = build_valve_payload(
            fields["mode"],
            fields["target"],
            fields["on_time"],
            fields["period"],
            fields["full_open"],
        )
        return payload

    def _hass_timer(
        self, delay: float, func: Callable[..., None], *args: Any
    ) -> None:
        """Запланировать отложенный вызов в event loop."""
        self.hass.loop.call_later(delay, func, *args)

    # --- Обработчики транспорта (вызываются в его потоке) -----------------

    def _on_client(self) -> None:
        """TLS-соединение установлено."""
        _LOGGER.info("Модуль подключился по TLS")
        if self.proxy:
            # Мост к облаку поднимается лениво — при первом подключении
            # модуля. Поднимать его нужно здесь, в потоке транспорта:
            # модуль шлёт регистрацию сразу после рукопожатия, и байты
            # не должны уйти в никуда.
            self._ensure_bridge()
        self._call_soon(self._set_available, True)

    def _make_bridge(self) -> ZotaCloudBridge:
        """Собрать мост к облаку (сеть не трогается)."""
        return ZotaCloudBridge(
            cloud_address=self.cloud_address,
            psk_hex=self.psk_hex,
            psk_identity=PSK_IDENTITY,
            on_frame=self._on_frame,
            send_to_module=self._send_to_module,
            get_registration_frame=self._get_registration_frame,
            on_registration_frame=self._on_registration_frame,
            logger=_LOGGER,
        )

    def _on_registration_frame(self, raw: bytes) -> None:
        """Сохранить сырой кадр регистрации модуля (link=2 op=2).

        Вызывается из потока моста, когда кадр регистрации проходит в
        направлении модуль → облако. Кадр сохраняется целиком, чтобы при
        подключении к облаку отправить его первым (см.
        :meth:`_get_registration_frame`).
        """
        with self._registration_lock:
            self._registration_frame = bytes(raw)

    def _get_registration_frame(self) -> bytes | None:
        """Отдать кэшированный кадр регистрации (или ``None``).

        Вызывается из потока моста сразу после рукопожатия с облаком:
        кэшированный кадр уходит первым, чтобы облако увидело серийник
        модуля. Если кэша нет — ничего не отправляется.
        """
        with self._registration_lock:
            return self._registration_frame

    def _ensure_bridge(self) -> ZotaCloudBridge | None:
        """Создать мост (если ещё нет) и поднять его поток.

        Вызывается из потока транспорта — поэтому только синхронные
        операции: создание объекта и запуск потока-демона.
        """
        if not self.proxy:
            return None
        bridge = self._bridge
        if bridge is None:
            bridge = self._make_bridge()
            self._bridge = bridge
        if not bridge.is_running:
            bridge.start()
            _LOGGER.warning("Мост к облаку ZOTA %s запущен", self.cloud_address)
        return bridge

    def _on_disconnect(self) -> None:
        """TLS-соединение разорвано."""
        _LOGGER.info("Модуль отключился")
        self._call_soon(self._handle_disconnected)

    def _on_frame(self, link: int, opcode: int, payload: bytes) -> None:
        """Разобранный кадр от модуля (поток транспорта)."""
        if link == LINK_REGISTER:
            if opcode == OPCODE_REGISTER:
                self._handle_register(payload)
            elif opcode == OPCODE_KEEPALIVE:
                # §11.3: на keepalive ответа не требуется — просто игнор.
                _LOGGER.debug("keepalive от модуля, ответа не требуется")
            else:
                _LOGGER.debug("Неизвестный кадр link=2 opcode=%d", opcode)
            return

        if link == LINK_FLAT and opcode == OPCODE_ACK:
            self._handle_ack(payload)
            return

        _LOGGER.debug("Неизвестный кадр link=%d opcode=%d", link, opcode)

    def _handle_register(self, payload: bytes) -> None:
        """Кадр регистрации ``link=2 op=2``.

        Payload (8 байт, §11.3): ``<u32 subtype LE><u32 serial LE>`` —
        например ``00000100 01000000`` = subtype 1, серийник 1 (пример).
        На регистрацию отвечать **нельзя** (§11.3, правило 1) — только
        запоминаем серийник.
        """
        if len(payload) < 8:
            _LOGGER.warning("Кадр регистрации слишком короткий: %d байт", len(payload))
            return
        serial = int.from_bytes(payload[4:8], "little")
        _LOGGER.info("Модуль подключён, серийник %d", serial)
        self._call_soon(self._set_serial, serial)

    def _handle_ack(self, payload: bytes) -> None:
        """Ответ модуля ``link=1 op=1``: ``<u16 cmd_echo><u16 result><data>``."""
        try:
            cmd_echo, result = parse_ack(payload)
        except ValueError as err:
            _LOGGER.warning("Некорректный ACK: %s", err)
            return

        future: concurrent.futures.Future[int] | None
        with self._pending_lock:
            future = self._pending.pop(cmd_echo, None)
        if future is not None and not future.done():
            future.set_result(result)

        if result == RESULT_NAK:
            _LOGGER.warning("Модуль ответил NAK на cmd=%d", cmd_echo)
            return

        if cmd_echo == CMD_INFO:
            data = payload[4:]
            if len(data) < 53:
                _LOGGER.warning("info-блок короткий: %d байт", len(data))
                return
            try:
                state = state_from_info(data, self.kw_per_stage)
            except ValueError as err:
                _LOGGER.warning("info-блок не разобран: %s", err)
                return
            self._call_soon(self._set_data, state)

    # --- Переходы в event loop --------------------------------------------

    def _handle_disconnected(self) -> None:
        """Обрыв соединения (в event loop)."""
        self._fail_pending()
        self._set_available(False)

    def _set_available(self, value: bool) -> None:
        """Обновить доступность и уведомить подписчиков (event loop).

        Доступность = есть связь с модулем. Пока связи нет, координатор
        считается неуспешным, и ``DataUpdateCoordinator.available`` (а значит
        и ``CoordinatorEntity.available``) возвращает False.
        """
        if self._connected == value:
            return
        self._connected = value
        self.last_update_success = value
        self.async_update_listeners()

    def _set_serial(self, serial: int) -> None:
        """Запомнить серийник модуля."""
        self._serial = serial
        self.async_update_listeners()

    def _set_data(self, state: ZotaState) -> None:
        """Сохранить новое состояние (event loop)."""
        self.async_set_updated_data(state)

    def _fail_pending(self) -> None:
        """Завершить все ожидающие записи ошибкой."""
        with self._pending_lock:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(
                        ConnectionError("соединение с модулем потеряно")
                    )
            self._pending.clear()

    def _call_soon(self, func: Callable[..., None], *args: Any) -> None:
        """Запланировать вызов в event loop из потока транспорта."""
        self.hass.loop.call_soon_threadsafe(func, *args)

    # --- Опрос ------------------------------------------------------------

    def _schedule_poll(self) -> None:
        """Перезапустить таймер опроса (event loop)."""
        if self._poll_timer is not None:
            self._poll_timer.cancel()
        self._poll_timer = self.hass.loop.call_later(POLL_INTERVAL, self._poll_tick)

    def _poll_tick(self) -> None:
        """Тик опроса: запросить info-блок, если модуль подключён."""
        try:
            if self._transport is not None and self._transport.is_connected:
                # Ретраи записи не должны блокировать event loop — отправка
                # уходит в executor пула по умолчанию (результат не ждём).
                self.hass.loop.run_in_executor(
                    None,
                    self._transport.send_frame,
                    LINK_FLAT,
                    OPCODE_COMMAND,
                    build_read(CMD_INFO)[HEADER_SIZE:],
                )
        finally:
            self._schedule_poll()


__all__ = [
    "WRITE_TIMEOUT",
    "ZotaConfigEntry",
    "ZotaCoordinator",
]
