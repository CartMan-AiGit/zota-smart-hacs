"""Select «Клапан: режим» ZOTA Smart (cmd=0x0C).

Запись — полный блок настроек клапана через
:meth:`ZotaCoordinator.send_valve_config` с одним изменённым полем ``mode``:
``0`` = Выключен, ``1`` = Тёплый пол, ``2`` = ЦО (§11.13.6). Mode-байт задаёт
контур и активирует его; целевая подставляется координатором автоматически
из контура назначения (mode=1 → ``[10]``, иначе ``[7]``). Текущее состояние —
поле ``[41]`` info-блока (``state.valve_mode``).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    VALVE_MODE_FLOOR,
    VALVE_MODE_HEATING,
    VALVE_MODE_OFF,
)
from .coordinator import ZotaConfigEntry, ZotaCoordinator
from .entity import ZotaEntity
from .state import ZotaState

#: Режим клапана → подпись в селекторе (порядок = порядок опций).
VALVE_MODE_NAMES: dict[int, str] = {
    VALVE_MODE_OFF: "Выключен",
    VALVE_MODE_FLOOR: "Тёплый пол",
    VALVE_MODE_HEATING: "ЦО",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZotaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Поднять select режима клапана для одной записи конфигурации."""
    coordinator: ZotaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZotaValveModeSelect(coordinator, VALVE_MODE_DESCRIPTION)])


VALVE_MODE_DESCRIPTION = SelectEntityDescription(
    key="valve_mode",
    translation_key="valve_mode",
    icon="mdi:tune-vertical",
)


class ZotaValveModeSelect(ZotaEntity, SelectEntity):
    """Select «Клапан: режим» (cmd=0x0C, поле mode)."""

    entity_description: SelectEntityDescription

    @property
    def _state(self) -> ZotaState | None:
        """Последнее состояние котла."""
        return self.coordinator.data

    @property
    def options(self) -> list[str]:
        """Опции селектора — подписи режимов."""
        return list(VALVE_MODE_NAMES.values())

    @property
    def current_option(self) -> str | None:
        """Текущий режим — поле ``[41]`` info-блока."""
        state = self._state
        if state is None:
            return None
        return VALVE_MODE_NAMES.get(state.valve_mode)

    async def async_select_option(self, option: str) -> None:
        """Записать режим клапана и дождаться подтверждения котла."""
        for mode, name in VALVE_MODE_NAMES.items():
            if name == option:
                break
        else:
            raise HomeAssistantError("неизвестный режим клапана: %s" % option)
        try:
            result = await self.coordinator.async_send_valve_config(mode=mode)
        except Exception as err:
            raise HomeAssistantError("котёл не подтвердил команду: %s" % err) from err
        if result != 0:
            raise HomeAssistantError("котёл не подтвердил команду")
        # Ускоренный опрос: обновлённое состояние нужно как можно скорее.
        await self.coordinator.async_poll_now()


__all__ = [
    "VALVE_MODE_DESCRIPTION",
    "VALVE_MODE_NAMES",
    "ZotaValveModeSelect",
    "async_setup_entry",
]
