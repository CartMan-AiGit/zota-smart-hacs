"""Мастер настройки интеграции ZOTA Smart."""

from __future__ import annotations

import logging
from typing import Any

import re

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    CONF_CLOUD_ADDRESS,
    CONF_DEVICE_PASSWORD,
    CONF_FRAME_LOG,
    CONF_NAME,
    CONF_PORT,
    CONF_POWER_KW,
    CONF_PROXY,
    CONF_PSK,
    CONF_SERIAL,
    CONF_TEMP_SENSOR,
    DEFAULT_CLOUD_ADDRESS,
    DEFAULT_FRAME_LOG,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_POWER_KW,
    DEFAULT_PSK,
    DEFAULT_PROXY,
    DEFAULT_TEMP_SENSOR,
    DOMAIN,
    POWER_KW_MAX,
    POWER_KW_MIN,
    POWER_KW_STEP,
    TEMP_SENSOR_OUTSIDE,
    TEMP_SENSOR_RETURN,
    TEMP_SENSOR_ROOM,
    TEMP_SENSOR_SUPPLY,
)

_LOGGER = logging.getLogger(__name__)

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """Общая валидация полей; возвращает словарь ошибок (пусто = OK)."""
    errors: dict[str, str] = {}
    port = user_input.get(CONF_PORT, DEFAULT_PORT)
    cloud_address = user_input.get(CONF_CLOUD_ADDRESS, DEFAULT_CLOUD_ADDRESS)
    psk = user_input.get(CONF_PSK) or ""
    power_kw = user_input.get(CONF_POWER_KW, DEFAULT_POWER_KW)
    if not 1 <= port <= 65535:
        errors[CONF_PORT] = "invalid_port"
    elif not cloud_address:
        errors[CONF_CLOUD_ADDRESS] = "invalid_cloud_address"
    elif not _HEX64_RE.match(psk.strip()):
        errors[CONF_PSK] = "invalid_psk"
    elif not POWER_KW_MIN <= power_kw <= POWER_KW_MAX:
        errors[CONF_POWER_KW] = "invalid_power_kw"
    elif power_kw % POWER_KW_STEP != 0:
        errors[CONF_POWER_KW] = "invalid_power_kw_step"
    return errors


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Нормализовать поля: PSK без пробелов, опциональные — только если заданы."""
    data = dict(user_input)
    if CONF_PSK in data and data[CONF_PSK] is not None:
        data[CONF_PSK] = str(data[CONF_PSK]).strip()
    return data


def _schema(current: dict[str, Any]) -> vol.Schema:
    """Схема формы по текущим значениям записи (data + options)."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(
                CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)
            ): int,
            vol.Required(
                CONF_PROXY, default=current.get(CONF_PROXY, DEFAULT_PROXY)
            ): bool,
            vol.Required(
                CONF_CLOUD_ADDRESS,
                default=current.get(CONF_CLOUD_ADDRESS, DEFAULT_CLOUD_ADDRESS),
            ): str,
            vol.Required(
                CONF_PSK, default=current.get(CONF_PSK, DEFAULT_PSK)
            ): str,
            vol.Required(
                CONF_POWER_KW,
                default=current.get(CONF_POWER_KW, DEFAULT_POWER_KW),
            ): int,
            vol.Required(
                CONF_TEMP_SENSOR,
                default=current.get(CONF_TEMP_SENSOR, DEFAULT_TEMP_SENSOR),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=TEMP_SENSOR_SUPPLY, label="Подача (датчик 3)"
                        ),
                        # значение ключа "return" не меняется (историческое),
                        # меняется только подпись: [15..16] — родной датчик в колбе
                        SelectOptionDict(
                            value=TEMP_SENSOR_RETURN,
                            label="Встроенный датчик (колба)",
                        ),
                        SelectOptionDict(
                            value=TEMP_SENSOR_ROOM, label="Датчик 1: помещение"
                        ),
                        SelectOptionDict(
                            value=TEMP_SENSOR_OUTSIDE, label="Датчик 2: улица"
                        ),
                    ],
                    mode="dropdown",
                )
            ),
            vol.Optional(CONF_SERIAL, description="серийник модуля"): int,
            vol.Optional(CONF_DEVICE_PASSWORD): str,
            vol.Required(
                CONF_FRAME_LOG, default=current.get(CONF_FRAME_LOG, DEFAULT_FRAME_LOG)
            ): bool,
        }
    )


class ZotaSmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Один шаг настройки: имя + расширенные параметры."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Показать форму и создать запись конфигурации."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _clean(user_input)
            errors = _validate(user_input)
            if not errors:
                name = user_input[CONF_NAME]
                await self.async_set_unique_id(name)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=name, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=_schema({}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "ZotaSmartOptionsFlow":
        """Вернуть options flow для этой записи."""
        return ZotaSmartOptionsFlow()


class ZotaSmartOptionsFlow(OptionsFlow):
    """Изменение настроек после создания записи."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Показать форму опций."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _clean(user_input)
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_schema(current), errors=errors
        )
