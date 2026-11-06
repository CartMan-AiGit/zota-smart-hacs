"""Чистая логика разбора info-блока ZOTA Smart в состояние.

Модуль намеренно самодостаточен: только стандартная библиотека, без
импорта .const и без homeassistant. Поэтому его можно использовать и
тестировать в одиночку, вне пакета интеграции.

Карта info-блока cmd=0 (53 байта данных) — PROTOCOL.md §11.8 + §11.12:

| Индекс | Поле |
|---|---|
| `[3]`  | текущая (активная) ступень (0…3) |
| `[4]`  | установленная ступень — уставка (0…3) |
| `[5]`  | уставка, °C (30…90) |
| `[15]` | встроенный датчик колбы, °C (живёт всегда, вне клемм 1-6) |
| `[17..18]` | датчик 1 «помещение», °C (u16 LE; 0x4001 = не подключён) |
| `[19..20]` | датчик 2 «улица», °C (u16 LE; 0x4001 = не подключён) |
| `[21]` | подача, °C (она же u16 LE ``[21..22]`` — датчик 3 «вода») |
| `[23..24]` | датчик 4 «воздух», °C (u16 LE; 0x4001 = не подключён) |
| `[29]` | насос 1: 1 = вкл, 2 = выкл |
| `[37]` | маска состояния (см. :data:`MASK_*`) |
| `[41]` | режим клапана (0 = выкл, 2 = ЦО) |
| `[43]` | клапан: включение, с |
| `[44..45]` | клапан: периодичность, с (u16 LE) |
| `[46..47]` | клапан: полное открытие, с (u16 LE) |
| `[48..52]` | коды ошибок (0 = нет ошибки) |

.. note::
   Раскладка ``[3]``/``[4]`` установлена **живой валидацией 2026-11-05**
   (веб ZOTA: уставка 15.0 кВт / текущая 0.0 кВт при ``[3]``=0, ``[4]``=3):
   ``[3]`` — **текущая (активная)** ступень, ``[4]`` — **установленная**
   (уставка-ограничитель). Статическая дока PROTOCOL.md была обратной.

Маска `[37]`:

| Бит | Значение |
|---|---|
| 0 | реле насоса 1 |
| 1 | реле насоса 2 |
| 5 | реле 1 (ТЭН) |
| 6 | реле 2 (ТЭН) |
| 7 | (не используется — нагрев определяется по активной ступени) |
"""

from __future__ import annotations

from dataclasses import dataclass

# Смещения полей в info-блоке (53 байта данных, индексы с 0).
#: ``[3]`` — **текущая (активная)** ступень. Живая валидация 2026-11-05.
OFF_STAGE_ACTIVE = 3
#: ``[4]`` — **установленная** ступень (уставка-ограничитель). Живая
#: валидация 2026-11-05.
OFF_STAGE_SELECTED = 4
OFF_SETPOINT = 5
OFF_TEMP_RETURN = 15
OFF_TEMP_SUPPLY = 21
OFF_PUMP1 = 29
OFF_MASK = 37
OFF_DIAG = 46
OFF_ERRORS_START = 48
OFF_ERRORS_END = 53

# --- Внешние датчики температуры (u16 LE, живая карта 2026-11-06) -----------
#: ``[17..18]`` — датчик 1, клемма «помещение».
OFF_SENSOR_ROOM = 17
#: ``[19..20]`` — датчик 2, клемма «улица».
OFF_SENSOR_OUTSIDE = 19
#: ``[23..24]`` — датчик 4, клемма «воздух». Слот ``[21..22]`` (датчик 3,
#: погружной «вода» в подаче) — это существующее поле ``temp_supply``.
OFF_SENSOR_AIR = 23
#: Значение слота u16 LE 0x4001 — датчик не подключён (→ ``None``).
SENSOR_ABSENT = 0x4001

# --- Поля клапана (§11.13, живой захват 2026-11-06) ------------------------
OFF_VALVE_TARGET = 7
#: Целевая температура контура «Тёплый пол» — поле ``[10]`` (§11.13.6).
OFF_VALVE_TARGET_FLOOR = 10
OFF_VALVE_MODE = 41
OFF_VALVE_ON_TIME = 43
OFF_VALVE_PERIOD = 44
OFF_VALVE_FULL_OPEN = 46

#: Полный размер info-блока (53 байта данных).
INFO_SIZE = 53

# Биты маски `[37]`.
MASK_PUMP1_RELAY = 0x01
MASK_PUMP2_RELAY = 0x02
MASK_RELAY1 = 0x20
MASK_RELAY2 = 0x40
#: Бит 7 маски `[37]`. Не используется как признак нагрева: живая валидация
#: 2026-11-05 показала, что при работающём котле бит 7 остаётся 0.
MASK_HEATING = 0x80

#: Значение `[29]` — насос 1 включён.
PUMP1_ON = 1
#: Значение `[29]` — насос 1 выключен.
PUMP1_OFF = 2

#: Ступени мощности по умолчанию: ступень × 5 = кВт (котёл 15 кВт).
#: Фактическая мощность ступени задаётся конфигурацией записи
#: (``kw_per_stage = power_kw // 3``).
KW_PER_STAGE = 5


@dataclass(frozen=True)
class ZotaState:
    """Состояние котла, разобранное из info-блока cmd=0."""

    #: Установленная ступень (уставка-ограничитель), поле ``[4]``.
    stage: int = 0
    #: Текущая (активная) ступень, поле ``[3]``.
    active_stage: int = 0
    setpoint: int = 0
    temp_return: int = 0
    temp_supply: int = 0
    pump1: bool = False
    pump2: bool = False
    heating: bool = False
    #: Бит 5 маски ``[37]`` — группа нагрева 10 кВт. Поле разбирается и
    #: хранится, но в панель не выводится: бинарных сенсоров только
    #: нагрев и насосы. Оставлено для отладки.
    relay1: bool = False
    #: Бит 6 маски ``[37]`` — группа нагрева 5 кВт (см. ``relay1``).
    relay2: bool = False
    raw37: int = 0
    #: Сырой байт режима насоса 1 — поле ``[29]`` info-блока без сведения.
    #: ``1`` — насос включён принудительно, ``2`` — не включён.
    #: Нужен switch'у насоса 1 для определения положения «вкл/выкл».
    pump1_mode_byte: int = 0
    #: Бит 0 маски ``[37]`` — реле насоса 1 включено (работает по автоматике
    #: либо по принудительному режиму).
    pump1_relay: bool = False
    diag: int = 0
    errors: tuple[int, ...] = ()
    #: Абсолютные индексы байт info-блока с ненулевыми кодами ошибок,
    #: параллельно кортежу ``errors``.
    error_offsets: tuple[int, ...] = ()
    #: Датчик 1, клемма «помещение» — u16 LE ``[17..18]``, °C;
    #: ``None`` — датчик не подключён (маркер 0x4001).
    sensor_room: float | None = None
    #: Датчик 2, клемма «улица» — u16 LE ``[19..20]``, °C; ``None`` — 0x4001.
    sensor_outside: float | None = None
    #: Датчик 4, клемма «воздух» — u16 LE ``[23..24]``, °C; ``None`` — 0x4001.
    sensor_air: float | None = None
    serial: int | None = None
    #: Режим клапана — поле ``[41]`` (0 = выкл, 2 = ЦО; §11.13.2).
    valve_mode: int = 0
    #: Целевая температура клапана — поле ``[7]``, °C.
    valve_target: int = 0
    #: Целевая температура контура «Тёплый пол» — поле ``[10]``, °C (§11.13.6).
    valve_target_floor: int = 0
    #: Клапан: время включения, с — поле ``[43]``.
    valve_on_time: int = 0
    #: Клапан: периодичность, с — u16 LE ``[44..45]``.
    valve_period: int = 0
    #: Клапан: полное открытие, с — u16 LE ``[46..47]``.
    valve_full_open: int = 0
    #: Мощность одной ступени, кВт (``power_kw // 3`` из конфигурации записи).
    #: Дефолт 5 — поведение прежних записей котла 15 кВт не меняется.
    kw_per_stage: int = KW_PER_STAGE

    @property
    def valve_target_active(self) -> int:
        """Целевая температура активного контура, °C (§11.13.6).

        В режиме «Тёплый пол» (``valve_mode == 1``) активная целевая — поле
        ``[10]``; во всех остальных (ЦО, выкл) — поле ``[7]``.
        """
        if self.valve_mode == 1:
            return self.valve_target_floor
        return self.valve_target

    @property
    def power_kw(self) -> int:
        """Мощность уставки (ограничителя), кВт.

        Уставка — установленная пользователем максимально допустимая
        ступень (поле ``[4]``), зеркало пресета климатической сущности. Котёл
        регулирует мощность по температуре: фактически включённых ТЭНов
        может быть меньше, чем разрешено уставкой.
        """
        return self.stage * self.kw_per_stage

    @property
    def power_active_kw(self) -> int:
        """Текущая (фактически выданная) мощность, кВт.

        Текущая (активная) ступень (поле ``[3]``) — это число реально
        включённых групп ТЭНов: при подходе к целевой температуре котёл
        снижает её 15 → 10 → 5 → 0, даже если уставка разрешает больше.
        """
        return self.active_stage * self.kw_per_stage

    @property
    def stage_name(self) -> str:
        """Название ступени: «Выкл» / «5 кВт» / … — по мощности ступени."""
        if self.stage <= 0:
            return "Выкл"
        return "%d кВт" % (self.stage * self.kw_per_stage)

    @property
    def has_errors(self) -> bool:
        """Есть ли хотя бы одна ошибка."""
        return bool(self.errors)

    @property
    def errors_summary(self) -> str:
        """Сводка по ошибкам: «ОК» либо «E48=3, E50=1».

        В подписи — абсолютный индекс байта info-блока, где лежит код.
        """
        if not self.errors:
            return "ОК"
        if len(self.error_offsets) == len(self.errors):
            pairs = zip(self.error_offsets, self.errors)
        else:
            # Кортеж собран вручную (без смещений) — отступаем от начала
            # диапазона ошибок.
            pairs = (
                (OFF_ERRORS_START + offset, code)
                for offset, code in enumerate(self.errors)
            )
        return ", ".join("E%d=%d" % (index, code) for index, code in pairs)


def _bit(mask: int, bit: int) -> bool:
    """Проверить бит в маске."""
    return bool(mask & bit)


def state_from_info(blob53: bytes, kw_per_stage: int = KW_PER_STAGE) -> ZotaState:
    """Разобрать info-блок cmd=0 (53 байта данных) в :class:`ZotaState`.

    Аргумент — именно блоб данных без 4-байтового заголовка ACK
    (``<u16 cmd_echo><u16 result>``).

    Приоритет по насосу 1: поле ``[29]`` (§11.12.2 — подтверждено живьём)
    важнее бита 0 маски ``[37]``. При конфликте (например ``[29]=2``, но
    бит 0 выставлен) считается, что выигрывает ``[29]`` — это прямое
    значение состояния насоса, тогда как бит 0 маски — производный признак
    реле, который может не успеть сброситься.

    Поле ``[29]``, равное чему-то, кроме 1 или 2, состояние насоса не
    меняет: в этом случае берётся бит 0 маски ``[37]``.

    Признак ``heating`` — **текущая (активная) ступень** ``[3]`` больше нуля,
    а не бит 7 маски ``[37]``: живая валидация 2026-11-05 показала, что при
    работающем котле (реле ТЭНов включены, вода греется) бит 7 остаётся 0,
    и индикатор «Нагрев» показывал «Не работает».

    Раскладка ступеней — по живой валидации 2026-11-05 (веб ZOTA): ``[3]`` —
    текущая (активная) ступень, ``[4]`` — установленная (уставка).
    """
    data = bytes(blob53)
    if len(data) < INFO_SIZE:
        raise ValueError(
            "info block too short: %d bytes (expected %d)" % (len(data), INFO_SIZE)
        )

    mask = data[OFF_MASK]
    pump1_raw = data[OFF_PUMP1]
    if pump1_raw == PUMP1_ON:
        pump1 = True
    elif pump1_raw == PUMP1_OFF:
        pump1 = False
    else:
        pump1 = _bit(mask, MASK_PUMP1_RELAY)

    errors = tuple(
        data[offset]
        for offset in range(OFF_ERRORS_START, OFF_ERRORS_END)
        if data[offset] != 0
    )
    #: Абсолютные индексы байтов с ненулевыми кодами ошибок — параллельно
    #: кортежу ``errors``, чтобы подпись ошибки ссылалась на реальный байт.
    error_offsets = tuple(
        offset
        for offset in range(OFF_ERRORS_START, OFF_ERRORS_END)
        if data[offset] != 0
    )

    #: Текущая (активная) ступень — поле ``[3]`` (живая валидация 2026-11-05).
    active_stage = data[3]
    #: Установленная ступень (уставка-ограничитель) — поле ``[4]``.
    stage = data[4]
    # Литералы выше обязаны совпадать с именованными смещениями.
    assert active_stage == data[OFF_STAGE_ACTIVE]
    assert stage == data[OFF_STAGE_SELECTED]

    def _slot(offset: int) -> float | None:
        """Прочитать слот внешнего датчика (u16 LE); 0x4001 → не подключён."""
        raw = int.from_bytes(data[offset:offset + 2], "little")
        if raw == SENSOR_ABSENT:
            return None
        return float(raw)

    return ZotaState(
        stage=stage,
        kw_per_stage=kw_per_stage,
        sensor_room=_slot(OFF_SENSOR_ROOM),
        sensor_outside=_slot(OFF_SENSOR_OUTSIDE),
        sensor_air=_slot(OFF_SENSOR_AIR),
        active_stage=active_stage,
        setpoint=data[OFF_SETPOINT],
        temp_return=data[OFF_TEMP_RETURN],
        temp_supply=data[OFF_TEMP_SUPPLY],
        pump1=pump1,
        pump2=_bit(mask, MASK_PUMP2_RELAY),
        heating=active_stage > 0,
        relay1=_bit(mask, MASK_RELAY1),
        relay2=_bit(mask, MASK_RELAY2),
        raw37=mask,
        pump1_mode_byte=pump1_raw,
        pump1_relay=_bit(mask, MASK_PUMP1_RELAY),
        diag=data[OFF_DIAG],
        errors=errors,
        error_offsets=error_offsets,
        valve_mode=data[OFF_VALVE_MODE],
        valve_target=data[OFF_VALVE_TARGET],
        valve_target_floor=data[OFF_VALVE_TARGET_FLOOR],
        valve_on_time=data[OFF_VALVE_ON_TIME],
        valve_period=int.from_bytes(data[OFF_VALVE_PERIOD:OFF_VALVE_PERIOD + 2], "little"),
        valve_full_open=int.from_bytes(
            data[OFF_VALVE_FULL_OPEN:OFF_VALVE_FULL_OPEN + 2], "little"
        ),
    )


__all__ = [
    "INFO_SIZE",
    "KW_PER_STAGE",
    "MASK_HEATING",
    "MASK_PUMP1_RELAY",
    "MASK_PUMP2_RELAY",
    "MASK_RELAY1",
    "MASK_RELAY2",
    "OFF_DIAG",
    "OFF_ERRORS_END",
    "OFF_ERRORS_START",
    "OFF_MASK",
    "OFF_PUMP1",
    "OFF_SENSOR_AIR",
    "OFF_SENSOR_OUTSIDE",
    "OFF_SENSOR_ROOM",
    "OFF_SETPOINT",
    "OFF_STAGE_ACTIVE",
    "OFF_STAGE_SELECTED",
    "OFF_TEMP_RETURN",
    "OFF_TEMP_SUPPLY",
    "OFF_VALVE_FULL_OPEN",
    "OFF_VALVE_MODE",
    "OFF_VALVE_ON_TIME",
    "OFF_VALVE_PERIOD",
    "OFF_VALVE_TARGET",
    "OFF_VALVE_TARGET_FLOOR",
    "PUMP1_OFF",
    "PUMP1_ON",
    "SENSOR_ABSENT",
    "ZotaState",
    "state_from_info",
]
