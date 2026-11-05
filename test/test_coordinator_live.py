#!/usr/bin/env python3
"""Standalone-тест класса ZotaCoordinator с подменёнными homeassistant-модулями.

Запуск (без pytest, без установленного homeassistant):

    python3 test/test_coordinator_live.py

Home Assistant не установлен локально, поэтому модуль ``homeassistant``
подменяется минимальными заглушками ДО импорта интеграции. При этом
проверяется настоящий код координатора: разбор кадров регистрации и ACK,
резолвинг Future на запись, таймаут записи, обработка обрыва.

Заглушки нужны только для ``homeassistant.config_entries``,
``homeassistant.core`` и ``homeassistant.helpers.update_coordinator``
(последний нужен только для ``entity.py``, который тут не импортируется).
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import types
from concurrent.futures import Future

# --- Заглушки homeassistant -------------------------------------------------

ha = types.ModuleType("homeassistant")
ha.__path__ = []  # type: ignore[attr-defined]

config_entries = types.ModuleType("homeassistant.config_entries")


class ConfigEntry:  # минимальная замена ConfigEntry
    def __init__(self, data=None, options=None, entry_id="test", title="ZOTA"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.title = title


config_entries.ConfigEntry = ConfigEntry  # type: ignore[attr-defined]
ha.config_entries = config_entries  # type: ignore[attr-defined]

core = types.ModuleType("homeassistant.core")


class HomeAssistant:
    """Заглушка: нужен только ``loop`` с ``call_soon_threadsafe``/``call_later``."""

    def __init__(self):
        self.loop = _FakeLoop()

    async def async_add_executor_job(self, func, *args):
        """Выполнить блокирующую функцию (в заглушке — сразу)."""
        return func(*args)


class _FakeLoop:
    """Простейший цикл: immediate-вызовы и таймеры с ручным прогоном."""

    def __init__(self):
        self._timers = []

    def call_soon_threadsafe(self, func, *args):
        func(*args)
        return None

    def call_later(self, delay, func, *args):
        handle = _Timer(delay, func, args)
        self._timers.append(handle)
        return handle

    def advance(self, seconds):
        """Прогнать таймеры, чей срок наступил."""
        due = [t for t in self._timers if t.delay <= seconds]
        for timer in due:
            self._timers.remove(timer)
            timer.func(*timer.args)


class _Timer:
    def __init__(self, delay, func, args):
        self.delay = delay
        self.func = func
        self.args = args

    def cancel(self):
        pass


core.HomeAssistant = HomeAssistant  # type: ignore[attr-defined]
ha.core = core  # type: ignore[attr-defined]


class DataUpdateCoordinator:
    """Минимальная замена DataUpdateCoordinator.

    В настоящем HA класс дженерик (``DataUpdateCoordinator[T]``) и хранит
    данные, слушателей и флаг ``last_update_success``.
    """

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True
        self._listeners = []

    def async_add_listener(self, update_callback, context=None):
        """Подписаться на обновления."""
        self._listeners.append(update_callback)

        def _remove():
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def async_update_listeners(self):
        """Уведомить подписчиков."""
        for listener in list(self._listeners):
            listener()

    @property
    def available(self):
        """Доступность координатора (по ``last_update_success``)."""
        return self.last_update_success

    def async_set_updated_data(self, data):
        """Сохранить новые данные и уведомить подписчиков."""
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()
        return data


helpers = types.ModuleType("homeassistant.helpers")
helpers.__path__ = []  # type: ignore[attr-defined]
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator  # type: ignore[attr-defined]
update_coordinator.CoordinatorEntity = object  # type: ignore[attr-defined]
helpers.update_coordinator = update_coordinator  # type: ignore[attr-defined]
ha.helpers = helpers  # type: ignore[attr-defined]

exceptions = types.ModuleType("homeassistant.exceptions")


class HomeAssistantError(Exception):
    """Замена HomeAssistantError."""


class ConfigEntryNotReady(HomeAssistantError):
    """Замена ConfigEntryNotReady."""


exceptions.HomeAssistantError = HomeAssistantError  # type: ignore[attr-defined]
exceptions.ConfigEntryNotReady = ConfigEntryNotReady  # type: ignore[attr-defined]
ha.exceptions = exceptions  # type: ignore[attr-defined]

sys.modules["homeassistant"] = ha
sys.modules["homeassistant.config_entries"] = config_entries
sys.modules["homeassistant.core"] = core
sys.modules["homeassistant.exceptions"] = exceptions
sys.modules["homeassistant.helpers"] = helpers
sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator

# --- Заглушка pyOpenSSL ----------------------------------------------------
# pyOpenSSL в тестовом окружении не установлен, а координатор импортирует
# его вместе с TLSServerTransport. Транспорт в тесте подменяется фейком, так
# что реальный OpenSSL не нужен — достаточно модуля с атрибутом SSL.

_openssl = types.ModuleType("OpenSSL")
_openssl.SSL = types.ModuleType("OpenSSL.SSL")  # type: ignore[attr-defined]
_openssl.__path__ = []  # type: ignore[attr-defined]
sys.modules["OpenSSL"] = _openssl
sys.modules["OpenSSL.SSL"] = _openssl.SSL

# --- Импорт интеграции -----------------------------------------------------

#: Корень пакета custom_components — чтобы работали относительные импорты
#: вида ``from .const import ...`` внутри модулей интеграции.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
    ),
)

from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402

from zota_smart.coordinator import WRITE_TIMEOUT, ZotaCoordinator  # noqa: E402

#: Серийник для тестовых кадров регистрации. По умолчанию — нейтральный
#: плейсхолдер; свой серийник можно задать переменной окружения
#: ``ZOTA_SERIAL``.
TEST_SERIAL: int = int(os.environ.get("ZOTA_SERIAL", "1"))

#: Кадр регистрации ``link=2 op=2``: ``<u32 subtype LE><u32 serial LE>``.
REGISTER_PAYLOAD: bytes = bytes.fromhex("00000100") + TEST_SERIAL.to_bytes(
    4, "little"
)


class FakeTransport:
    """Подмена TLS-транспорта: запоминает отправленные кадры."""

    def __init__(self):
        self.sent = []
        self.is_connected = True
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout=5.0):
        self.stopped = True

    def send_frame(self, link, opcode, payload=b""):
        self.sent.append((link, opcode, bytes(payload)))
        return True


def check(condition, message):
    """Проверка с падением при ошибке."""
    if not condition:
        raise AssertionError(message)


def make_coordinator():
    """Собрать координатор с фейковым транспортом."""
    hass = HomeAssistant()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
    transport = FakeTransport()
    coordinator = ZotaCoordinator(hass, entry, transport=transport)
    return coordinator, transport, hass


def case_register() -> None:
    """Кадр регистрации → сохранённый серийник, лог, device_identifier."""
    coordinator, _transport, _hass = make_coordinator()
    coordinator._on_frame(2, 2, REGISTER_PAYLOAD)
    print(f"--- case 1: регистрация ---\n  serial = {coordinator.serial}")
    check(
        coordinator.serial == TEST_SERIAL,
        f"serial: ожидалось {TEST_SERIAL}, получено {coordinator.serial}",
    )
    check(
        coordinator.device_identifier == "boiler",
        f"device_identifier: получено {coordinator.device_identifier}",
    )
    print("  case 1: OK\n")


def case_keepalive() -> None:
    """Keepalive не меняет состояние и не отправляет ответ."""
    coordinator, transport, _hass = make_coordinator()
    coordinator._on_frame(2, 2, REGISTER_PAYLOAD)
    before = (coordinator.serial, coordinator.data, coordinator.connected)
    coordinator._on_frame(2, 4, b"")
    print(
        f"--- case 2: keepalive ---\n  serial = {coordinator.serial}\n"
        f"  sent frames = {len(transport.sent)}\n  connected = {coordinator.connected}"
    )
    check(len(transport.sent) == 0, "keepalive: ответ отправлять нельзя")
    check(
        (coordinator.serial, coordinator.data, coordinator.connected) == before,
        "keepalive: состояние измениться не должно",
    )
    print("  case 2: OK\n")


def case_info_ack() -> None:
    """ACK с info-блоком → coordinator.data."""
    coordinator, _transport, _hass = make_coordinator()
    info = bytearray(53)
    info[5] = 61
    info[15] = 20
    info[21] = 21
    info[29] = 1
    info[37] = 0x11
    info[46] = 180
    payload = struct.pack("<HH", 0, 0) + bytes(info)
    coordinator._on_frame(1, 1, payload)
    state = coordinator.data
    print(
        f"--- case 3: ACK info ---\n  setpoint = {state.setpoint}\n"
        f"  t_supply = {state.temp_supply}\n  t_return = {state.temp_return}\n"
        f"  pump1    = {state.pump1}\n  heating  = {state.heating}"
    )
    check(state is not None, "state: ожидалось не-None")
    check(state.setpoint == 61, f"setpoint: ожидалось 61, получено {state.setpoint}")
    check(state.temp_supply == 21, f"temp_supply: ожидалось 21, получено {state.temp_supply}")
    check(state.temp_return == 20, f"temp_return: ожидалось 20, получено {state.temp_return}")
    check(state.pump1 is True, f"pump1: ожидалось True, получено {state.pump1}")
    check(state.heating is False, f"heating: ожидалось False, получено {state.heating}")
    print("  case 3: OK\n")


def case_write_ack() -> None:
    """Запись → Future резолвится значением result из ACK."""
    coordinator, transport, _hass = make_coordinator()
    future = coordinator.send_write(2, 57)
    print(f"--- case 4: запись ---\n  sent = {[(l, o, p.hex()) for l, o, p in transport.sent]}")
    check(len(transport.sent) == 1, "ожидался один отправленный кадр")
    link, opcode, payload = transport.sent[0]
    check(link == 1, f"link: ожидалось 1, получено {link}")
    check(opcode == 0, f"opcode: ожидалось 0, получено {opcode}")
    check(payload == struct.pack("<HH", 2, 57), f"payload: получено {payload.hex()}")
    check(not future.done(), "Future не должен быть завершён до ACK")
    coordinator._on_frame(1, 1, struct.pack("<HH", 2, 0))
    print(f"  future.result() = {future.result()}")
    check(future.done(), "Future должен быть завершён после ACK")
    check(future.result() == 0, f"result: ожидалось 0, получено {future.result()}")
    print("  case 4: OK\n")


def case_write_timeout() -> None:
    """Запись без ACK → таймаут через WRITE_TIMEOUT."""
    coordinator, _transport, hass = make_coordinator()
    future = coordinator.send_write(1, 2)
    print(f"--- case 5: таймаут записи ---\n  WRITE_TIMEOUT = {WRITE_TIMEOUT}")
    check(not future.done(), "Future не должен быть завершён до таймаута")
    hass.loop.advance(WRITE_TIMEOUT + 0.1)
    print(f"  future.done() = {future.done()}\n  exception = {future.exception()!r}")
    check(future.done(), "Future должен быть завершён по таймауту")
    check(isinstance(future.exception(), TimeoutError), "ожидался TimeoutError")
    print("  case 5: OK\n")


def case_disconnect() -> None:
    """Обрыв соединения → available=False, pending Future завершается ошибкой."""
    coordinator, _transport, _hass = make_coordinator()
    coordinator._on_client()
    check(coordinator.connected is True, "после on_client ожидалось connected=True")
    future = coordinator.send_write(1, 2)
    coordinator._on_disconnect()
    print(
        f"--- case 6: обрыв ---\n  connected = {coordinator.connected}\n"
        f"  exception = {future.exception()!r}"
    )
    check(coordinator.connected is False, "после обрыва ожидалось connected=False")
    check(future.done(), "pending Future должен быть завершён")
    check(isinstance(future.exception(), ConnectionError), "ожидался ConnectionError")
    print("  case 6: OK\n")


def case_poll() -> None:
    """Тик опроса при подключённом модуле шлёт запрос cmd=0."""
    coordinator, transport, hass = make_coordinator()
    coordinator._poll_tick()
    print(f"--- case 7: опрос ---\n  sent = {[(l, o, p.hex()) for l, o, p in transport.sent]}")
    check(len(transport.sent) == 1, "ожидался один отправленный кадр")
    link, opcode, payload = transport.sent[0]
    check((link, opcode) == (1, 0), f"ожидался кадр (1, 0), получен ({link}, {opcode})")
    check(payload == struct.pack("<H", 0), f"payload: получено {payload.hex()}")
    print("  case 7: OK\n")


def case_listeners() -> None:
    """Подписчики уведомляются об изменениях состояния."""
    coordinator, _transport, _hass = make_coordinator()
    calls = []
    unsub = coordinator.async_add_listener(lambda: calls.append(1))
    coordinator._on_client()
    info = bytearray(53)
    info[5] = 50
    coordinator._on_frame(1, 1, struct.pack("<HH", 0, 0) + bytes(info))
    print(f"--- case 8: подписчики ---\n  уведомлений = {len(calls)}")
    check(len(calls) >= 2, f"ожидалось ≥2 уведомлений, получено {len(calls)}")
    unsub()
    before = len(calls)
    coordinator._on_disconnect()
    check(len(calls) == before, "после отписки уведомлений быть не должно")
    print("  case 8: OK\n")


def case_psk_required() -> None:
    """Без PSK запуск падает с ConfigEntryNotReady."""
    hass = HomeAssistant()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
    coordinator = ZotaCoordinator(hass, entry, transport=FakeTransport())
    check(
        coordinator.psk_hex == "",
        "без поля psk ключ должен быть пустым",
    )
    try:
        asyncio.run(coordinator.async_start())
    except ConfigEntryNotReady as err:
        print(f"--- case 9: PSK обязателен ---\n  ConfigEntryNotReady: {err}")
        check(
            "Укажите PSK" in str(err),
            f"текст ошибки должен подсказывать про PSK, получено {err!r}",
        )
    else:
        raise AssertionError("ожидался ConfigEntryNotReady без PSK")

    # С ключом запуск проходит.
    entry2 = ConfigEntry(data={"name": "Котёл", "port": 1978, "psk": "11" * 32})
    coordinator2 = ZotaCoordinator(hass, entry2, transport=FakeTransport())
    asyncio.run(coordinator2.async_start())
    print("  case 9: OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_coordinator_live: настоящий код ZotaCoordinator ===\n")
    case_register()
    case_keepalive()
    case_info_ack()
    case_write_ack()
    case_write_timeout()
    case_disconnect()
    case_poll()
    case_listeners()
    case_psk_required()
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
