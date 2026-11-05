"""Интеграция ZOTA Smart для Home Assistant.

Фаза 4: координатор с TLS-PSK транспортом, read-сущности (sensor,
binary_sensor) и управляющие сущности (climate, switch): запись ступени
(cmd=1), уставки (cmd=2), состояния насоса 1 (cmd=0x0A) и насоса 2
(cmd=0x0B).

Режим PROXY (``proxy: true`` в настройках) — мост к настоящему облаку ZOTA
(:mod:`.proxy`): байты LAN-модуля прозрачно пересылаются в облако и
обратно, состояние HA при этом обновляется локально.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, POLL_INTERVAL
from .coordinator import ZotaConfigEntry, ZotaCoordinator

_LOGGER = logging.getLogger(__name__)

#: Платформы, которые поднимаются для каждой записи конфигурации.
PLATFORMS: list[str] = [
    "climate",
    "switch",
    "sensor",
    "binary_sensor",
    "number",
    "select",
]


async def async_migrate_entry(hass: HomeAssistant, entry: ZotaConfigEntry) -> bool:
    """v1 → v2: новые поля (psk, power_kw, …) опциональны.

    Данные не меняются: отсутствующий ``power_kw`` даёт
    ``DEFAULT_POWER_KW`` (пресеты 15-кВт котла не изменяются).
    Отсутствующий ``psk`` требует перенастройки записи — без ключа
    координатор не поднимется (``ConfigEntryNotReady``).
    """
    if entry.version > 2:
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ZotaConfigEntry) -> bool:
    """Настроить одну запись конфигурации."""
    coordinator = ZotaCoordinator(hass, entry)
    await coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info(
        "ZOTA Smart '%s' запущен: порт=%s, опрос=%s c",
        coordinator.name,
        coordinator.port,
        POLL_INTERVAL,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZotaConfigEntry) -> bool:
    """Выгрузить одну запись конфигурации."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: ZotaCoordinator | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.async_stop()
    _LOGGER.info("ZOTA Smart '%s' выгружен", entry.title)
    return unloaded


__all__ = [
    "DOMAIN",
    "PLATFORMS",
    "ZotaConfigEntry",
    "ZotaCoordinator",
    "async_setup_entry",
    "async_unload_entry",
]
