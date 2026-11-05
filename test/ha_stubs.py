#!/usr/bin/env python3
"""Общие заглушки Home Assistant для standalone-тестов интеграции.

Home Assistant не установлен локально, поэтому модуль ``homeassistant``
подменяется минимальными заглушками ДО импорта платформ. Здесь собрано
всё общее для тестов: ``homeassistant.core``,
``homeassistant.helpers.*``, ``homeassistant.components.climate``,
``homeassistant.components.switch``, ``homeassistant.exceptions``.

Импорт этого модуля имеет побочный эффект — регистрацию заглушек в
``sys.modules``; он должен выполняться **до** импорта пакетов
``zota_smart.*``.
"""

from __future__ import annotations

import asyncio
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
        self.loop = FakeLoop()

    async def async_add_executor_job(self, func, *args):
        """Выполнить блокирующую функцию (в заглушке — сразу)."""
        return func(*args)


_mod("homeassistant.core", HomeAssistant=HomeAssistant)


class EntityDescription:
    """Минимальная замена EntityDescription."""

    def __init__(self, key, name=None, device_class=None, state_class=None,
                 native_unit_of_measurement=None, icon=None):
        self.key = key
        self.name = name
        self.device_class = device_class
        self.state_class = state_class
        self.native_unit_of_measurement = native_unit_of_measurement
        self.icon = icon


class Entity:
    """Минимальная замена Entity: ``_attr_unique_id`` → свойство ``unique_id``."""

    _attr_unique_id: str | None = None

    @property
    def unique_id(self):
        """Идентификатор сущности."""
        return self._attr_unique_id


class DataUpdateCoordinator:
    """Минимальная замена DataUpdateCoordinator."""

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
    """Минимальная замена CoordinatorEntity (подписка + координатор)."""

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


class HomeAssistantError(Exception):
    """Замена HomeAssistantError."""


class ConfigEntryNotReady(HomeAssistantError):
    """Замена ConfigEntryNotReady: запись ещё не настроена, setup будет повторён."""


_mod(
    "homeassistant.exceptions",
    HomeAssistantError=HomeAssistantError,
    ConfigEntryNotReady=ConfigEntryNotReady,
)


class HVACMode:
    """Замена HVACMode."""

    OFF = "off"
    HEAT = "heat"


class ClimateEntityFeature:
    """Замена ClimateEntityFeature (битовые флаги возможностей)."""

    TARGET_TEMPERATURE = 1
    PRESET_MODE = 8


class ClimateEntity:
    """Минимальная замена ClimateEntity."""

    _attr_hvac_modes: list = []
    _attr_temperature_unit: str | None = None
    _attr_min_temp: float | None = None
    _attr_max_temp: float | None = None
    _attr_target_temperature_step: float | None = None
    _attr_preset_modes: list = []
    _attr_supported_features = 0

    @property
    def hvac_modes(self):
        """Список доступных режимов."""
        return self._attr_hvac_modes

    @property
    def temperature_unit(self):
        """Единица измерения температуры."""
        return self._attr_temperature_unit

    @property
    def min_temp(self):
        """Минимальная уставка."""
        return self._attr_min_temp

    @property
    def max_temp(self):
        """Максимальная уставка."""
        return self._attr_max_temp

    @property
    def target_temperature_step(self):
        """Шаг уставки."""
        return self._attr_target_temperature_step

    @property
    def preset_modes(self):
        """Список пресетов."""
        return self._attr_preset_modes

    @property
    def supported_features(self):
        """Поддерживаемые возможности."""
        return self._attr_supported_features


class ClimateEntityDescription(EntityDescription):
    """Описание климатической сущности."""


_mod("homeassistant.components")
_mod("homeassistant.components.climate")
_mod(
    "homeassistant.components.climate",
    ClimateEntity=ClimateEntity,
    ClimateEntityDescription=ClimateEntityDescription,
    ClimateEntityFeature=ClimateEntityFeature,
    HVACMode=HVACMode,
)


class SwitchEntity:
    """Минимальная замена SwitchEntity."""

    _attr_is_on: bool | None = None

    @property
    def is_on(self):
        """Состояние выключателя."""
        return self._attr_is_on


class SwitchEntityDescription(EntityDescription):
    """Описание switch-сущности."""


# --- Заглушки number / select / const ---------------------------------------


class UnitOfTemperature:
    """Замена UnitOfTemperature."""

    CELSIUS = "°C"


class UnitOfTime:
    """Замена UnitOfTime."""

    SECONDS = "s"


_mod("homeassistant.const", UnitOfTemperature=UnitOfTemperature, UnitOfTime=UnitOfTime)


class NumberDeviceClass:
    """Замена NumberDeviceClass."""

    TEMPERATURE = "temperature"


class NumberMode:
    """Замена NumberMode."""

    BOX = "box"
    SLIDER = "slider"


class NumberEntity:
    """Минимальная замена NumberEntity (значения — из description)."""

    _attr_native_value: float | None = None
    _attr_mode: str = NumberMode.BOX

    @property
    def mode(self):
        """Режим отображения (box/slider)."""
        return self._attr_mode

    @property
    def native_min_value(self):
        """Минимум слайдера."""
        return getattr(self.entity_description, "native_min_value", None)

    @property
    def native_max_value(self):
        """Максимум слайдера."""
        return getattr(self.entity_description, "native_max_value", None)

    @property
    def native_step(self):
        """Шаг слайдера."""
        return getattr(self.entity_description, "native_step", None)

    @property
    def native_unit_of_measurement(self):
        """Единица измерения."""
        return getattr(self.entity_description, "native_unit_of_measurement", None)

    @property
    def device_class(self):
        """Класс устройства."""
        return getattr(self.entity_description, "device_class", None)


class NumberEntityDescription(EntityDescription):
    """Минимальная замена NumberEntityDescription (с границами)."""

    def __init__(self, key, name=None, device_class=None,
                 native_unit_of_measurement=None, icon=None,
                 native_min_value=None, native_max_value=None, native_step=None):
        super().__init__(
            key,
            name=name,
            device_class=device_class,
            native_unit_of_measurement=native_unit_of_measurement,
            icon=icon,
        )
        self.native_min_value = native_min_value
        self.native_max_value = native_max_value
        self.native_step = native_step


_mod("homeassistant.components.number")
_mod(
    "homeassistant.components.number",
    NumberDeviceClass=NumberDeviceClass,
    NumberEntity=NumberEntity,
    NumberMode=NumberMode,
    NumberEntityDescription=NumberEntityDescription,
)


class SelectEntity:
    """Минимальная замена SelectEntity."""

    _attr_current_option: str | None = None

    @property
    def current_option(self):
        """Текущая опция."""
        return self._attr_current_option


class SelectEntityDescription(EntityDescription):
    """Минимальная замена SelectEntityDescription."""


_mod("homeassistant.components.select")
_mod(
    "homeassistant.components.select",
    SelectEntity=SelectEntity,
    SelectEntityDescription=SelectEntityDescription,
)


_mod("homeassistant.components.switch")
_mod(
    "homeassistant.components.switch",
    SwitchEntity=SwitchEntity,
    SwitchEntityDescription=SwitchEntityDescription,
)


class BinarySensorDeviceClass:
    """Замена BinarySensorDeviceClass (битовые флаги классов)."""

    RUNNING = "running"
    PROBLEM = "problem"


class BinarySensorEntity:
    """Минимальная замена BinarySensorEntity."""

    _attr_is_on: bool | None = None

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
# через tls_server и proxy. Реальный OpenSSL здесь не нужен: тесты моста
# подменяют TLS-соединение фейком и проверяют маршрутизацию байтов.


class _FakeSSLContext:
    """Минимальная замена ``SSL.Context``."""

    def __init__(self, method=None):
        self.method = method
        self.cipher_list = None
        self.min_version = None
        self.max_version = None

    def set_cipher_list(self, cipher_list):
        """Запомнить список шифров."""
        self.cipher_list = cipher_list

    def set_min_proto_version(self, version):
        """Запомнить минимальную версию протокола."""
        self.min_version = version

    def set_max_proto_version(self, version):
        """Запомнить максимальную версию протокола."""
        self.max_version = version


class _FakeSSLModule:
    """Минимальная замена ``OpenSSL.SSL``."""

    Error = type("Error", (Exception,), {})
    WantReadError = type("WantReadError", (Error,), {})
    WantWriteError = type("WantWriteError", (Error,), {})
    ZeroReturnError = type("ZeroReturnError", (Error,), {})
    # pyOpenSSL поднимает системную ошибку сокета (EAGAIN и т. п.) как
    # SysCallError — подкласс SSL.Error. Нужен, чтобы проверить, что
    # EAGAIN не рвёт соединение.
    SysCallError = type("SysCallError", (Error,), {})
    TLS_CLIENT_METHOD = object()
    TLS_SERVER_METHOD = object()
    TLS1_2_VERSION = 0x0303
    Context = _FakeSSLContext
    Connection = object


_openssl = types.ModuleType("OpenSSL")
_openssl.SSL = _FakeSSLModule  # type: ignore[attr-defined]
_openssl.__path__ = []  # type: ignore[attr-defined]
sys.modules["OpenSSL"] = _openssl
sys.modules["OpenSSL.SSL"] = _openssl.SSL


class FakeLoop:
    """Цикл с immediate-выполнением."""

    def call_soon_threadsafe(self, func, *args):
        """Вызвать функцию сразу; корутину — прогнать до конца."""
        result = func(*args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
        return None

    def call_later(self, delay, func, *args):
        return None


class FakeHass:
    """Заглушка hass для координатора."""

    def __init__(self):
        self.loop = FakeLoop()

    async def async_add_executor_job(self, func, *args):
        """Выполнить блокирующую функцию (в заглушке — сразу)."""
        return func(*args)


def check(condition, message):
    """Проверка с падением при ошибке."""
    if not condition:
        raise AssertionError(message)


__all__ = [
    "ClimateEntity",
    "ClimateEntityDescription",
    "ClimateEntityFeature",
    "ConfigEntry",
    "ConfigEntryNotReady",
    "Entity",
    "EntityDescription",
    "FakeHass",
    "FakeLoop",
    "HomeAssistantError",
    "HVACMode",
    "SwitchEntity",
    "SwitchEntityDescription",
    "check",
]
