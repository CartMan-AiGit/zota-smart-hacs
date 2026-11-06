#!/usr/bin/env python3
"""Standalone-тест платформ sensor/binary_sensor ZOTA Smart.

Запуск (без pytest, без установленного homeassistant):

    python3 test/test_entities.py

Home Assistant не установлен локально, поэтому модуль ``homeassistant``
подменяется минимальными заглушками ДО импорта платформ. Проверяется
настоящий код платформ: значения нативных сенсоров, бинарные признаки,
единое устройство и уникальные идентификаторы.
"""

from __future__ import annotations

import os
import sys
import types

# --- Заглушки homeassistant -------------------------------------------------

ha = types.ModuleType("homeassistant")
ha.__path__ = []  # type: ignore[attr-defined]


def _mod(name, **attrs):
    """Создать модуль-заглушку с набором атрибутов."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class ConfigEntry:
    """Минимальная замена ConfigEntry."""

    def __init__(self, data=None, options=None, entry_id="test", title="ZOTA"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.title = title


_mod("homeassistant.config_entries", ConfigEntry=ConfigEntry)


class HomeAssistant:
    """Заглушка HA."""

    def __init__(self):
        self.data = {}


_mod("homeassistant.core", HomeAssistant=HomeAssistant)


class HomeAssistantError(Exception):
    """Минимальная замена HomeAssistantError."""


class ConfigEntryNotReady(HomeAssistantError):
    """Минимальная замена ConfigEntryNotReady."""


_mod(
    "homeassistant.exceptions",
    HomeAssistantError=HomeAssistantError,
    ConfigEntryNotReady=ConfigEntryNotReady,
)


class EntityDescription:
    """Минимальная замена EntityDescription."""

    def __init__(self, key, name=None, device_class=None, state_class=None,
                 native_unit_of_measurement=None, icon=None, translation_key=None):
        self.key = key
        self.name = name
        self.translation_key = translation_key
        self.device_class = device_class
        self.state_class = state_class
        self.native_unit_of_measurement = native_unit_of_measurement
        self.icon = icon


class Entity:
    """Минимальная замена Entity: ``_attr_unique_id`` → свойство ``unique_id``.

    В настоящем HA ``Entity`` именно так и отдаёт ``unique_id`` — через
    свойство поверх ``_attr_unique_id``.
    """

    _attr_unique_id: str | None = None

    @property
    def unique_id(self):
        """Идентификатор сущности."""
        return self._attr_unique_id


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

    def async_set_updated_data(self, data):
        """Сохранить новые данные и уведомить подписчиков."""
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()
        return data


class CoordinatorEntity(Entity):
    """Минимальная замена CoordinatorEntity (подписка + координатор).

    В настоящем HA класс дженерик (``CoordinatorEntity[T]``), поэтому
    заглушка тоже должна поддерживать подписку параметром типа. Наследует
    ``Entity``, чтобы работал ``_attr_unique_id`` → ``unique_id``.
    """

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator, context=None):
        super().__init__()
        self.coordinator = coordinator
        self.entity_description = None

    async def async_added_to_hass(self):
        pass


_mod("homeassistant.helpers")
_mod(
    "homeassistant.helpers.entity",
    EntityDescription=EntityDescription,
    Entity=Entity,
)
_mod(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=CoordinatorEntity,
    DataUpdateCoordinator=DataUpdateCoordinator,
)
_mod(
    "homeassistant.helpers.device_registry",
    DeviceInfo=dict,
)
_mod(
    "homeassistant.helpers.entity_platform",
    AddEntitiesCallback=object,
)


class SensorDeviceClass:
    TEMPERATURE = "temperature"
    POWER = "power"


class SensorStateClass:
    MEASUREMENT = "measurement"


class SensorEntity(Entity):
    """Минимальная замена SensorEntity.

    В настоящем HA ``SensorEntity`` — платформенная база, через которую
    ``native_value`` маршрутизируется в состояние сенсора.
    """

    _attr_native_value = None

    @property
    def native_value(self):
        """Нативное значение сенсора."""
        return self._attr_native_value


class SensorEntityDescription(EntityDescription):
    """Описание сенсора."""


_mod(
    "homeassistant.components",
)
_mod("homeassistant.components.sensor")
_mod(
    "homeassistant.components.sensor",
    SensorDeviceClass=SensorDeviceClass,
    SensorEntity=SensorEntity,
    SensorStateClass=SensorStateClass,
    SensorEntityDescription=SensorEntityDescription,
)


class BinarySensorDeviceClass:
    RUNNING = "running"


class BinarySensorEntity(Entity):
    """Минимальная замена BinarySensorEntity.

    В настоящем HA ``BinarySensorEntity`` — платформенная база, через которую
    ``is_on`` маршрутизируется в состояние бинарного сенсора.
    """

    _attr_is_on = None

    @property
    def is_on(self):
        """Состояние бинарного сенсора."""
        return self._attr_is_on


class BinarySensorEntityDescription(EntityDescription):
    """Описание бинарного сенсора."""


_mod("homeassistant.components.binary_sensor")
_mod(
    "homeassistant.components.binary_sensor",
    BinarySensorDeviceClass=BinarySensorDeviceClass,
    BinarySensorEntity=BinarySensorEntity,
    BinarySensorEntityDescription=BinarySensorEntityDescription,
)

# --- Заглушка pyOpenSSL ----------------------------------------------------
# pyOpenSSL в тестовом окружении не установлен, а пакет интеграции тянет его
# через tls_server. Реальный OpenSSL здесь не нужен.

_openssl = types.ModuleType("OpenSSL")
_openssl.SSL = types.ModuleType("OpenSSL.SSL")  # type: ignore[attr-defined]
_openssl.__path__ = []  # type: ignore[attr-defined]
sys.modules["OpenSSL"] = _openssl
sys.modules["OpenSSL.SSL"] = _openssl.SSL

# --- Импорт интеграции -----------------------------------------------------

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
    ),
)

from zota_smart import binary_sensor, sensor  # noqa: E402
from zota_smart.coordinator import ZotaCoordinator  # noqa: E402
from zota_smart.state import state_from_info  # noqa: E402


class FakeLoop:
    """Цикл с immediate-выполнением."""

    def call_soon_threadsafe(self, func, *args):
        func(*args)

    def call_later(self, delay, func, *args):
        return None


class FakeHass:
    """Заглушка hass для координатора."""

    def __init__(self):
        self.loop = FakeLoop()


def check(condition, message):
    """Проверка с падением при ошибке."""
    if not condition:
        raise AssertionError(message)


def make_coordinator():
    """Собрать координатор с фейковым транспортом и заданным состоянием."""
    hass = FakeHass()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
    coordinator = ZotaCoordinator(hass, entry, transport=object())
    return coordinator


#: Серийник для тестовых device_info. По умолчанию — нейтральный
#: плейсхолдер; свой серийник можно задать переменной окружения
#: ``ZOTA_SERIAL``.
TEST_SERIAL: int = int(os.environ.get("ZOTA_SERIAL", "1"))


def make_info(
    stage=1,
    active_stage=None,
    setpoint=61,
    t_return=20,
    t_supply=21,
    pump1=1,
    mask=0x11,
    diag=180,
):
    """Собрать info-блок с заданными полями.

    Раскладка ступеней — по живой валидации 2026-11-05 (веб ZOTA):
    ``[3]`` — **текущая (активная)** ступень, ``[4]`` — **установленная**
    (уставка). Поэтому ``stage`` (уставка) кладётся в ``[4]``, а
    ``active_stage`` (текущая) — в ``[3]``.

    ``active_stage`` по умолчанию равен ``stage`` (текущая ступень совпадает
    с уставкой); задаётся отдельно, чтобы проверить признак «Нагрев».
    """
    if active_stage is None:
        active_stage = stage
    info = bytearray(53)
    info[3] = active_stage
    info[4] = stage
    info[5] = setpoint
    info[15] = t_return
    info[21] = t_supply
    info[29] = pump1
    info[37] = mask
    info[46] = diag
    return bytes(info)


def build_sensor(coordinator, key):
    """Создать сенсор по ключу описания."""
    for description in sensor.SENSOR_DESCRIPTIONS:
        if description.key == key:
            return sensor.ZotaTemperatureSensor(coordinator, description)
    raise KeyError(key)


def build_binary(coordinator, key):
    """Создать бинарный сенсор по ключу описания."""
    for description in binary_sensor.BINARY_SENSOR_DESCRIPTIONS:
        if description.key == key:
            return binary_sensor.ZotaBinarySensor(coordinator, description)
    raise KeyError(key)


def case_sensor_values() -> None:
    """Значения нативных сенсоров."""
    coordinator = make_coordinator()
    coordinator._set_data(state_from_info(make_info()))
    print("--- case 1: сенсоры ---")
    for key in (
        "temp_supply",
        "temp_return",
        "setpoint",
        "stage",
        "power_active_kw",
        "power_kw",
        "errors",
    ):
        entity = build_sensor(coordinator, key)
        value = entity.native_value
        print(f"  {key:16s} = {value!r}")
    check(build_sensor(coordinator, "temp_supply").native_value == 21, "подача: ожидалось 21")
    check(build_sensor(coordinator, "temp_return").native_value == 20, "колба: ожидалось 20")
    check(build_sensor(coordinator, "setpoint").native_value == 61, "уставка: ожидалось 61")
    check(build_sensor(coordinator, "stage").native_value == "5 кВт", "ступень: ожидалось «5 кВт»")
    check(
        build_sensor(coordinator, "power_kw").native_value == 5,
        "мощность (уставка): ожидалось 5",
    )
    check(
        build_sensor(coordinator, "power_active_kw").native_value == 5,
        "мощность (текущая): ожидалось 5",
    )
    check(build_sensor(coordinator, "errors").native_value == "ОК", "ошибки: ожидалось «ОК»")
    print("  case 1: OK\n")


def case_power_split() -> None:
    """Мощность разделена: текущая — из ``[3]``, уставка — из ``[4]``.

    Котёл регулирует нагрев по целевой температуре: уставка разрешает
    15 кВт, а фактически включённых ТЭНов может быть 10, 5 или ни разу.
    Раскладка — по живой валидации 2026-11-05 (веб ZOTA).
    """
    coordinator = make_coordinator()
    coordinator._set_data(state_from_info(make_info(stage=3, active_stage=2)))
    current = build_sensor(coordinator, "power_active_kw").native_value
    limit = build_sensor(coordinator, "power_kw").native_value
    print(
        f"--- case 13: разделение мощностей ---\n"
        f"  active_stage=[3]=2, stage=[4]=3\n"
        f"  текущая = {current!r}, уставка = {limit!r}"
    )
    check(current == 10, f"текущая: ожидалось 10, получено {current!r}")
    check(limit == 15, f"уставка: ожидалось 15, получено {limit!r}")

    # Подход к уставке: мощность падает 15 → 10 → 5 → 0 при неизменной уставке.
    for active, expected in ((3, 15), (2, 10), (1, 5), (0, 0)):
        coordinator._set_data(state_from_info(make_info(stage=3, active_stage=active)))
        current = build_sensor(coordinator, "power_active_kw").native_value
        limit = build_sensor(coordinator, "power_kw").native_value
        print(f"  active_stage={active} → текущая={current}, уставка={limit}")
        check(current == expected, f"текущая при active={active}: ожидалось {expected}")
        check(limit == 15, f"уставка при active={active}: ожидалось 15")
    print("  case 13: OK\n")


def case_sensor_empty() -> None:
    """Без данных — значение None."""
    coordinator = make_coordinator()
    entity = build_sensor(coordinator, "setpoint")
    print(f"--- case 2: нет данных ---\n  native_value = {entity.native_value!r}")
    check(entity.native_value is None, "ожидалось None без данных")
    print("  case 2: OK\n")


def case_binary_values() -> None:
    """Значения бинарных сенсоров."""
    coordinator = make_coordinator()
    coordinator._set_data(state_from_info(make_info(mask=0xE3, pump1=1)))
    print("--- case 3: бинарные сенсоры ---")
    for key in ("heating", "pump1", "pump2"):
        entity = build_binary(coordinator, key)
        value = entity.is_on
        print(f"  {key:8s} = {value!r}")
    check(build_binary(coordinator, "heating").is_on is True, "нагрев: ожидалось True")
    check(build_binary(coordinator, "pump1").is_on is True, "насос 1: ожидалось True")
    check(build_binary(coordinator, "pump2").is_on is True, "насос 2: ожидалось True")
    print("  case 3: OK\n")


def case_binary_pump1_conflict() -> None:
    """Насос 1: приоритет [29]=2 над битом 0 маски."""
    coordinator = make_coordinator()
    coordinator._set_data(state_from_info(make_info(pump1=2, mask=0x11)))
    value = build_binary(coordinator, "pump1").is_on
    print(f"--- case 4: насос 1, [29]=2 при bit0=1 ---\n  is_on = {value!r}")
    check(value is False, "ожидалось False (приоритет [29])")
    print("  case 4: OK\n")


def case_binary_heating() -> None:
    """Нагрев = текущая (активная) ступень ``[3]`` > 0, бит 7 маски не учитывается.

    Живая валидация 2026-11-05: котёл греет (реле ТЭНов работают, вода
    греется), а бит 7 маски ``[37]`` = 0 — индикатор показывал «Не работает».
    """
    coordinator = make_coordinator()
    coordinator._set_data(
        state_from_info(make_info(stage=1, active_stage=1, mask=0x01))
    )
    value = build_binary(coordinator, "heating").is_on
    print(f"--- case 12: нагрев, active_stage=1 при bit7=0 ---\n  is_on = {value!r}")
    check(value is True, "ожидалось True (active_stage=1)")

    coordinator._set_data(
        state_from_info(make_info(stage=0, active_stage=0, mask=0x81))
    )
    value = build_binary(coordinator, "heating").is_on
    print(f"  active_stage=0 при bit7=1 → is_on = {value!r}")
    check(value is False, "ожидалось False (active_stage=0)")

    # Живой кейс 2026-11-05 (веб ZOTA): уставка 15 кВт ([4]=3), текущая 0 кВт
    # ([3]=0) → «Нагрев» выключен, «Мощность: уставка» 15, «текущая» 0.
    coordinator._set_data(
        state_from_info(make_info(stage=3, active_stage=0, mask=0x00))
    )
    value = build_binary(coordinator, "heating").is_on
    current = build_sensor(coordinator, "power_active_kw").native_value
    limit = build_sensor(coordinator, "power_kw").native_value
    stage_text = build_sensor(coordinator, "stage").native_value
    print(
        f"  уставка 3 ([4]=3), текущая 0 ([3]=0) → нагрев={value!r}, "
        f"текущая={current!r}, уставка={limit!r}, ступень={stage_text!r}"
    )
    check(value is False, "ожидалось False (текущая ступень = 0)")
    check(current == 0, f"мощность (текущая): ожидалось 0, получено {current!r}")
    check(limit == 15, f"мощность (уставка): ожидалось 15, получено {limit!r}")
    check(stage_text == "15 кВт", f"ступень: ожидалось «15 кВт», получено {stage_text!r}")
    print("  case 12: OK\n")


def case_device_and_unique_id() -> None:
    """Единое устройство и уникальные идентификаторы."""
    coordinator = make_coordinator()
    coordinator._set_serial(TEST_SERIAL)
    entity = build_sensor(coordinator, "setpoint")
    device = entity.device_info
    print(f"--- case 5: устройство ---\n  identifiers = {device['identifiers']}")
    print(f"  name         = {device['name']}")
    print(f"  unique_id    = {entity.unique_id}")
    check(
        device["identifiers"] == {("zota_smart", "boiler")},
        f"identifiers: получено {device['identifiers']}",
    )
    check(device["name"] == "ZOTA Smart", f"name: получено {device['name']}")
    check(
        entity.unique_id == "boiler_setpoint",
        f"unique_id: получено {entity.unique_id}",
    )
    check(
        device["serial_number"] == str(TEST_SERIAL),
        f"serial_number: получено {device['serial_number']}",
    )
    binary_entity = build_binary(coordinator, "heating")
    check(
        binary_entity.device_info["identifiers"] == device["identifiers"],
        "все сущности должны быть в одном устройстве",
    )
    print("  case 5: OK\n")


def case_device_pending() -> None:
    """До регистрации — тот же постоянный идентификатор (без дублей)."""
    coordinator = make_coordinator()
    entity = build_sensor(coordinator, "setpoint")
    device = entity.device_info
    print(
        f"--- case 6: до регистрации ---\n  identifiers = {device['identifiers']}\n"
        f"  unique_id    = {entity.unique_id}"
    )
    check(
        device["identifiers"] == {("zota_smart", "boiler")},
        f"identifiers: получено {device['identifiers']}",
    )
    check(
        entity.unique_id == "boiler_setpoint",
        f"unique_id: получено {entity.unique_id}",
    )
    print("  case 6: OK\n")


def case_identifier_stable_across_registration() -> None:
    """Идентичность не меняется при получении серийника — дублей нет."""
    coordinator = make_coordinator()
    before = build_sensor(coordinator, "setpoint")
    before_ids = before.device_info["identifiers"]
    before_uid = before.unique_id
    coordinator._set_serial(TEST_SERIAL)
    after = build_sensor(coordinator, "setpoint")
    print(
        f"--- case 11: идентичность до/после регистрации ---\n"
        f"  identifiers до = {before_ids}, после = {after.device_info['identifiers']}\n"
        f"  unique_id  до = {before_uid}, после = {after.unique_id}"
    )
    check(
        before_ids == after.device_info["identifiers"],
        "identifiers не должны меняться после регистрации",
    )
    check(
        before_uid == after.unique_id,
        "unique_id не должен меняться после регистрации",
    )
    print("  case 11: OK\n")


def case_available() -> None:
    """available следует за связью с модулем."""
    coordinator = make_coordinator()
    entity = build_sensor(coordinator, "setpoint")
    print(f"--- case 7: available ---\n  available (offline) = {entity.available}")
    check(entity.available is False, "ожидалось available=False без связи")
    coordinator._on_client()
    print(f"  available (online)  = {entity.available}")
    check(entity.available is True, "ожидалось available=True при подключении")
    coordinator._on_disconnect()
    print(f"  available (offline) = {entity.available}")
    check(entity.available is False, "ожидалось available=False после обрыва")
    print("  case 7: OK\n")


def case_data_update() -> None:
    """Состояние доставляется через async_set_updated_data."""
    coordinator = make_coordinator()
    coordinator._set_data(state_from_info(make_info()))
    print(f"--- case 9: async_set_updated_data ---\n  data.setpoint = {coordinator.data.setpoint}")
    check(coordinator.data is not None, "ожидалось не-Nile data")
    check(coordinator.data.setpoint == 61, f"setpoint: получено {coordinator.data.setpoint}")
    print("  case 9: OK\n")


def case_sensor_meta() -> None:
    """Метаданные сенсоров: device_class, единицы измерения."""
    coordinator = make_coordinator()
    print("--- case 8: метаданные ---")
    by_key = {d.key: d for d in sensor.SENSOR_DESCRIPTIONS}
    check(by_key["temp_supply"].device_class == "temperature", "подача: device_class")
    check(by_key["temp_supply"].native_unit_of_measurement == "°C", "подача: единицы")
    check(by_key["temp_return"].device_class == "temperature", "колба: device_class")
    check(by_key["setpoint"].device_class == "temperature", "уставка: device_class")
    check(by_key["power_kw"].device_class == "power", "мощность (уставка): device_class")
    check(by_key["power_kw"].native_unit_of_measurement == "kW", "мощность (уставка): единицы")
    check(
        by_key["power_active_kw"].device_class == "power",
        "мощность (текущая): device_class",
    )
    check(
        by_key["power_active_kw"].native_unit_of_measurement == "kW",
        "мощность (текущая): единицы",
    )
    # Имена сущностей — через translations (translation_key), см. ru.json.
    check(
        by_key["power_active_kw"].translation_key == "power_active_kw",
        f"tkey текущей: {by_key['power_active_kw'].translation_key!r}",
    )
    check(
        by_key["power_kw"].translation_key == "power_kw",
        f"tkey уставки: {by_key['power_kw'].translation_key!r}",
    )
    for key, description in by_key.items():
        print(f"  {key:12s} class={description.device_class} unit={description.native_unit_of_measurement}")
    print("  case 8: OK\n")


def case_ext_sensors() -> None:
    """Внешние датчики температуры: слоты u16 LE [17]/[19]/[23], маркер 0x4001.

    Живой кадр 2026-11-06 04:46-05:03: датчик 1 (помещение) = 24,
    датчик 2 (улица) = 41, датчик 3 (подача, слот [21..22]) = 62,
    датчик 4 (воздух) = не подключён (0x4001).
    """
    coordinator = make_coordinator()
    info = bytearray(53)
    info[4] = 3
    info[17:19] = (24).to_bytes(2, "little")   # помещение
    info[19:21] = (41).to_bytes(2, "little")   # улица
    info[21:23] = (62).to_bytes(2, "little")   # вода (подача)
    info[23:25] = (0x4001).to_bytes(2, "little")  # воздух: не подключён
    coordinator._set_data(state_from_info(bytes(info)))
    print("--- case: внешние датчики ---")
    for key in ("sensor_room", "sensor_outside", "sensor_air"):
        value = build_sensor(coordinator, key).native_value
        print(f"  {key:16s} = {value!r}")
    check(build_sensor(coordinator, "sensor_room").native_value == 24.0,
          "датчик помещение: ожидался 24.0")
    check(build_sensor(coordinator, "sensor_outside").native_value == 41.0,
          "датчик улица: ожидался 41.0")
    check(build_sensor(coordinator, "sensor_air").native_value is None,
          "датчик воздух: 0x4001 → None")
    check(build_sensor(coordinator, "temp_supply").native_value == 62,
          "подача из слота датчика 3: ожидалось 62")
    print("  case: внешние датчики OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_entities: платформы sensor/binary_sensor ===\n")
    case_sensor_values()
    case_sensor_empty()
    case_ext_sensors()
    case_binary_values()
    case_binary_pump1_conflict()
    case_binary_heating()
    case_device_and_unique_id()
    case_device_pending()
    case_available()
    case_sensor_meta()
    case_data_update()
    case_identifier_stable_across_registration()
    case_power_split()
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
