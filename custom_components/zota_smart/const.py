"""Константы протокола ZOTA PO Smart v2 (облачный/локальный канал :1978).

Все значения выверены по реверс-инжинирингу — PROTOCOL.md §11.
"""

from typing import Final

DOMAIN: Final = "zota_smart"
NAME: Final = "ZOTA Smart"
VERSION: Final = "0.1.0"

#: Постоянный идентификатор устройства. Не зависит от серийника: серийник
#: приходит уже после создания сущностей, и смена идентификатора заставила
#: бы HA создать второй набор сущностей. Серийник доступен как
#: ``serial_number`` в ``device_info``.
DEVICE_IDENTIFIER: Final = "boiler"

# --- Транспорт (§11.2) -----------------------------------------------------

#: TLS-PSK pre-shared key, 32 байта (hex). Принадлежит владельцу оборудования.
#: Пусто: PSK обязателен и вводится пользователем в config flow.
#: Как получить ключ своего модуля — см. раздел README «Получение PSK».
DEFAULT_PSK: Final = ""

#: PSK identity в TLS-PSK handshake.
PSK_IDENTITY: Final = "Smart key"

#: TCP-порт LAN-модуля котла (TLS-PSK) и облачного канала.
PORT: Final = 1978

#: Родное облако ZOTA (режим PROXY).
CLOUD_HOST: Final = "217.25.88.41"
CLOUD_PORT: Final = 1978
CLOUD_ADDRESS: Final = f"{CLOUD_HOST}:{CLOUD_PORT}"

#: Домен, на который котёл ходит сам; требуется DNS override на IP HA.
CONTROL_DOMAIN: Final = "control.zota.ru"

#: Интервал опроса телеметрии, с (§11.11).
POLL_INTERVAL: Final = 10

# --- Формат кадра (§11.1, §11.7) ------------------------------------------

#: link=1 — плоский командный кадр.
LINK_FLAT: Final = 1
#: link=2 — регистрация модуля в облаке (§11.3).
LINK_REGISTER: Final = 2

#: opcode=0 — команда/запись.
OPCODE_COMMAND: Final = 0
#: opcode=1 — ответ модуля (ACK).
OPCODE_ACK: Final = 1
#: opcode=2 — регистрация (link=2).
OPCODE_REGISTER: Final = 2
#: opcode=4 — keepalive (link=2, каждые 30 с, без ответа).
OPCODE_KEEPALIVE: Final = 4

#: Размер заголовка кадра: link, opcode, len (3 × u16 LE).
HEADER_SIZE: Final = 6

# --- Карта команд (§11.7) --------------------------------------------------

#: cmd=0 — info, 53 байта телеметрии (read).
CMD_INFO: Final = 0
#: cmd=1 — ступень/режим мощности, диапазон 0…3 (read/write).
CMD_STAGE: Final = 1
#: cmd=2 — уставка температуры, °C (read/write).
CMD_SETPOINT: Final = 2
#: cmd=16 — паспорт, 7 байт (read).
CMD_PASSPORT: Final = 16
#: cmd=0x0A — параметр насоса. ВНИМАНИЕ: не картирован в PROTOCOL.md §11.7
#: (диапазон «3…0x13 — прочие параметры БУ» помечен как неразобранный).
#: Подтверждён эмпирически 2026-11-04: TX 0100000004000a000101 → ACK 0a000000 result=0.
#: Эндиан write-значения — см. :data:`PUMP1_MODE_ON`/:data:`PUMP1_MODE_OFF`.
CMD_PUMP1: Final = 0x0A

#: cmd=0x0B — параметр насоса 2. Как и у насоса 1, не картирован в
#: PROTOCOL.md §11.7 (подтверждено живьём 2026-11-04).
#: В отличие от насоса 1 запись 6-байтная: ``<u16 cmd><u8 mode><u8 0><u8 0><u8 0>``.
CMD_PUMP2: Final = 0x0B

#: cmd=0x0C — настройки клапана (§11.13). Write — полный блок 11 байт:
#: ``<u16 0x0C><u8 режим><u8 0x01><u8 целевая><u8 25><u8 включение>
#: <u16 периодичность LE><u16 полное_открытие LE>``.
CMD_VALVE: Final = 0x0C

#: Режим клапана «Выключен» (info ``[41]``, write-байт ``[2]``).
VALVE_MODE_OFF: Final = 0
#: Режим клапана «Тёплый пол» — по индукции, живьём не подтверждён (§11.13.1).
VALVE_MODE_FLOOR: Final = 1
#: Режим клапана «ЦО».
VALVE_MODE_HEATING: Final = 2

#: Константа write-блока клапана `[3]` — всегда ``01`` (§11.13.1).
VALVE_CONST_FLAG: Final = 0x01
#: Константа write-блока клапана `[5]` — всегда ``0x19`` = 25 (§11.13.1).
VALVE_CONST_TEMP2: Final = 25

#: Диапазоны настроек клапана (§11.13).
VALVE_TARGET_MIN: Final = 20
VALVE_TARGET_MAX: Final = 90
#: Потолок целевой для контура «Тёплый пол» (mode=1): target>50 → ACK
#: result=1, изменения нет (захват 2026-11-06, §11.13.6).
VALVE_TARGET_FLOOR_MAX: Final = 50
VALVE_ON_TIME_MIN: Final = 3
VALVE_ON_TIME_MAX: Final = 60
VALVE_PERIOD_MIN: Final = 10
VALVE_PERIOD_MAX: Final = 600
VALVE_FULL_OPEN_MIN: Final = 30
VALVE_FULL_OPEN_MAX: Final = 600

#: Диапазоны значений write-параметров (§11.7).
STAGE_MIN: Final = 0
STAGE_MAX: Final = 3
SETPOINT_MIN: Final = 30
SETPOINT_MAX: Final = 90

# --- Climate / switch (§11.7, §11.12) ---------------------------------------

def build_stage_presets(power_kw: int) -> tuple[str, ...]:
    """Пресеты климата по мощности котла: «Выкл» + ступени P/3, 2P/3, P кВт.

    Индекс пресета совпадает с номером ступени для записи cmd=1. Для 15 кВт
    даёт прежний статический набор («Выкл» / «5 кВт» / «10 кВт» / «15 кВт»).
    """
    return (
        "Выкл",
        "%d кВт" % (power_kw // 3),
        "%d кВт" % (2 * power_kw // 3),
        "%d кВт" % power_kw,
    )

#: Режим насоса 1 «вкл» в write-кадре cmd=0x0A (§11.12.1).
#:
#: Write-значение cmd=0x0A — это u16 **little-endian**, у которого ПЕРВЫЙ байт
#: (младший) — режим, ВТОРОЙ — таймаут:
#: ``val = mode | (timeout << 8)``.
#: Доказательство: боевой кадр облака ``0a000201`` — байты ``02 01``:
#: режим 2 («выкл») идёт первым, таймаут 1 — вторым. Поэтому «выкл» =
#: ``0x0102``, а не ``0x0201`` (последний кодирует режим 1 = «вкл»).
PUMP1_MODE_ON: Final = 1
#: Режим насоса 1 «выкл» в write-кадре cmd=0x0A (§11.12.1).
#: В write-значении это ``0x0102``: байты ``02 01`` (режим 2, таймаут 1).
PUMP1_MODE_OFF: Final = 2
#: Режим насоса 1 «авто» в write-кадре cmd=0x0A (§11.12.1).
#: В write-значении это ``0x0100``: байты ``00 01`` (режим 0, таймаут 1).
PUMP1_MODE_AUTO: Final = 0

#: Таймаут работы насоса 1, минут (§11.12.1: диапазон 1…120).
PUMP1_TIMEOUT_MIN: Final = 1

#: Готовое write-значение cmd=0x0A для режима «вкл»: ``0x0101``
#: (байты ``01 01`` — режим 1, таймаут 1).
PUMP1_VAL_ON: Final = 0x0101
#: Готовое write-значение cmd=0x0A для режима «выкл»: ``0x0102``
#: (байты ``02 01`` — режим 2, таймаут 1).
PUMP1_VAL_OFF: Final = 0x0102
#: Готовое write-значение cmd=0x0A для режима «авто»: ``0x0100``
#: (байты ``00 01`` — режим 0, таймаут 1).
PUMP1_VAL_AUTO: Final = 0x0100

#: Готовый payload записи насоса 2 «Вкл»: ``<u16 0x0B><u8 mode=0><0><0><0>``.
#: Доказано 2026-11-04: TX frame(1,0, 0b0000000000) → ACK cmd_echo=0x0B.
PUMP2_PAYLOAD_ON: Final = bytes.fromhex("0b0000000000")

#: Готовый payload записи насоса 2 «Выкл»: ``<u16 0x0B><u8 mode=1><0><0><0>``.
#: Доказано 2026-11-04: TX frame(1,0, 0b0001000000) → ACK cmd_echo=0x0B.
PUMP2_PAYLOAD_OFF: Final = bytes.fromhex("0b0001000000")

#: Смещения полей в info-блоке cmd=0 (§11.8).
#:
#: Живая валидация 2026-11-05 (веб ZOTA): ``[3]`` — **текущая** (активная)
#: ступень, ``[4]`` — **установленная** (уставка-ограничитель). Статическая
#: дока была обратной — см. PROTOCOL.md §11.8.
INFO_OFFSET_STAGE_ACTIVE: Final = 3
INFO_OFFSET_STAGE_SELECTED: Final = 4
INFO_OFFSET_SETPOINT: Final = 5
INFO_OFFSET_TEMP_RETURN: Final = 15
INFO_OFFSET_TEMP_SUPPLY: Final = 21
#: Внешние датчики температуры — u16 LE (§11.8, живая карта 2026-11-06):
#: ``[17..18]`` клемма «помещение», ``[19..20]`` клемма «улица»,
#: ``[23..24]`` клемма «воздух». Слот ``[21..22]`` (погружной «вода» в
#: подаче) уже публикуется сенсором «Датчик 3: вода (подача)». Клеммы 5/6
#: (ГВС/ГР) слота
#: в info-блоке нет.
INFO_OFFSET_SENSOR_ROOM: Final = 17
INFO_OFFSET_SENSOR_OUTSIDE: Final = 19
INFO_OFFSET_SENSOR_AIR: Final = 23
#: Маркер «датчик не подключён» в слоте внешнего датчика (u16 LE 0x4001).
SENSOR_ABSENT: Final = 0x4001
INFO_OFFSET_POWER_RAW: Final = 37
INFO_OFFSET_STATUS: Final = 46
#: Полный размер info-блока (53 байта данных + 4 байта заголовка ACK).
INFO_PAYLOAD_SIZE: Final = 57
#: Размер блока паспорта (7 байт данных + 4 байта заголовка ACK).
PASSPORT_PAYLOAD_SIZE: Final = 11

# --- Коды результата ACK (§11.3) ------------------------------------------

RESULT_OK_DATA: Final = 0
RESULT_OK_EMPTY: Final = 1
RESULT_NAK: Final = 2

# --- Значения по умолчанию для config flow --------------------------------

DEFAULT_NAME: Final = "Котёл"
DEFAULT_PORT: Final = PORT
DEFAULT_PROXY: Final = False
DEFAULT_CLOUD_ADDRESS: Final = CLOUD_ADDRESS
DEFAULT_POWER_KW: Final = 15

#: Диапазон мощности семейства Smart, кВт (шаг 3: 3, 6, … 30).
POWER_KW_MIN: Final = 3
POWER_KW_MAX: Final = 30
POWER_KW_STEP: Final = 3

# --- Ключи конфигурации (config flow / options flow) -----------------------

CONF_NAME: Final = "name"
CONF_PORT: Final = "port"
CONF_PROXY: Final = "proxy"
CONF_CLOUD_ADDRESS: Final = "cloud_address"
CONF_PSK: Final = "psk"
CONF_POWER_KW: Final = "power_kw"
CONF_SERIAL: Final = "serial"
CONF_DEVICE_PASSWORD: Final = "device_password"
CONF_TEMP_SENSOR: Final = "temp_sensor"

# --- Источник текущей температуры climate-сущности --------------------------

#: Вариант «Подача» — датчик 3 «вода», слот ``[21..22]`` (как исторически).
TEMP_SENSOR_SUPPLY: Final = "supply"
#: Вариант «Встроенный датчик (колба)» — поле ``[15..16]``: родной датчик
#: в колбе котла (бывшая provisional-метка «обратка» снята, f21).
TEMP_SENSOR_RETURN: Final = "return"
#: Вариант «Датчик 1: помещение» — слот ``[17..18]``; 0x4001 → None.
TEMP_SENSOR_ROOM: Final = "room"
#: Вариант «Датчик 2: улица» — слот ``[19..20]``; 0x4001 → None.
TEMP_SENSOR_OUTSIDE: Final = "outside"
#: Дефолт при отсутствии поля в entry — подача (поведение не меняется).
DEFAULT_TEMP_SENSOR: Final = TEMP_SENSOR_SUPPLY


def mask_psk(psk: str) -> str:
    """Маскированный PSK для логов: первые 6 символов + «…»."""
    return psk[:6] + "…"
