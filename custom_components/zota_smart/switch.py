"""Switch-насосы ZOTA Smart: насос 1 и насос 2 (только вкл/выкл).

Насосы управляются как обычные выключатели: никаких селекторов и режимов
«Авто» — только включение и выключение.

**Насос 1** — запись ``cmd=0x0A``, значение ``mode | (timeout << 8)``:
это u16 **little-endian**, у которого ПЕРВЫЙ (младший) байт — режим,
ВТОРОЙ — таймаут. ``mode``: 1 = вкл, 2 = выкл, а ``timeout`` — минуты
работы (1…120) — PROTOCOL.md §11.12.1. Доказательство: боевой кадр облака
``0a000201`` — байты ``02 01`` (режим 2 идёт первым). Поэтому «вкл» =
``0x0101`` (байты ``01 01``), «выкл» = ``0x0102`` (байты ``02 01``).

**Насос 2** — запись ``cmd=0x0B``, 6-байтный payload
``<u16 cmd><u8 mode><u8 0><u8 0><u8 0>``, где ``mode``: 0 = вкл, 1 = выкл
(подтверждено живьём 2026-11-04).

Состояние насоса 1 берётся из поля ``[29]`` info-блока
(``state.pump1_mode_byte``): значение 1 — насос включён принудительно.

Состояние насоса 2 берётся из бита 1 маски ``[37]`` (``state.pump2``).

.. note::
   Реле насоса может работать дольше, чем держится выбранный режим: после
   команды котёл продолжает прогон насос по постциркуляции и охлаждению.
   Признак фактической работы реле — в бинарных сенсорах (см.
   :mod:`.binary_sensor`).
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CMD_PUMP1,
    DOMAIN,
    PUMP1_MODE_OFF,
    PUMP1_MODE_ON,
    PUMP1_TIMEOUT_MIN,
    PUMP2_PAYLOAD_OFF,
    PUMP2_PAYLOAD_ON,
)
from .coordinator import ZotaConfigEntry, ZotaCoordinator
from .entity import ZotaEntity
from .state import PUMP1_ON, ZotaState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZotaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Поднять switch насосов 1 и 2 для одной записи конфигурации."""
    coordinator: ZotaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ZotaPump1Switch(coordinator, PUMP1_DESCRIPTION),
            ZotaPump2Switch(coordinator, PUMP2_DESCRIPTION),
        ]
    )


PUMP1_DESCRIPTION = SwitchEntityDescription(
    key="pump1",
    translation_key="pump1",
    icon="mdi:pump",
)

PUMP2_DESCRIPTION = SwitchEntityDescription(
    key="pump2",
    translation_key="pump2",
    icon="mdi:pump",
)


class ZotaPump1Switch(ZotaEntity, SwitchEntity):
    """Switch насоса 1 ZOTA Smart (cmd=0x0A)."""

    entity_description: SwitchEntityDescription

    @property
    def _state(self) -> ZotaState | None:
        """Последнее состояние котла."""
        return self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        """Включён ли насос 1.

        Состояние — поле ``[29]`` info-блока (``state.pump1_mode_byte``):
        значение 1 означает принудительно включённый насос.

        Реле при этом может продолжать работать и после выключения команды
        (постциркуляция/охлаждение) — фактическую работу реле показывает
        бинарный сенсор.
        """
        state = self._state
        if state is None:
            return None
        return state.pump1_mode_byte == PUMP1_ON

    async def async_turn_on(self, **kwargs) -> None:
        """Включить насос 1 (cmd=0x0A, mode=1 → val=0x0101)."""
        await self._write(PUMP1_MODE_ON)

    async def async_turn_off(self, **kwargs) -> None:
        """Выключить насос 1 (cmd=0x0A, mode=2 → val=0x0102)."""
        await self._write(PUMP1_MODE_OFF)

    async def _write(self, mode: int) -> None:
        """Записать режим насоса 1 и дождаться подтверждения котла.

        Write-значение cmd=0x0A — u16 LE, у которого ПЕРВЫЙ байт — режим,
        ВТОРОЙ — таймаут: ``val = mode | (timeout << 8)``. «Выкл» = ``0x0102``
        (байты ``02 01``), а не ``0x0201`` — тот кодировал бы режим 1 («вкл»),
        из-за чего выключить насос было невозможно.
        """
        value = mode | (PUMP1_TIMEOUT_MIN << 8)
        try:
            result = await self.coordinator.async_send_write(CMD_PUMP1, value)
        except Exception as err:
            raise HomeAssistantError("котёл не подтвердил команду: %s" % err) from err
        if result != 0:
            raise HomeAssistantError("котёл не подтвердил команду")
        # Ускоренный опрос: обновлённое состояние нужно как можно скорее.
        await self.coordinator.async_poll_now()


class ZotaPump2Switch(ZotaEntity, SwitchEntity):
    """Switch насоса 2 ZOTA Smart (cmd=0x0B).

    У насоса 2 только два положения — «Вкл» и «Выкл» (у насоса 1 в
    исходном протоколе был ещё режим «Авто», но в switch он не выражается).

    **Замечание:** режимы 2 и 3 (как у насоса 1 — «авто»/таймер) для насоса 2
    не подтверждены — ГВС/ЦО добавим после верификации mode 2/3.
    """

    entity_description: SwitchEntityDescription

    @property
    def _state(self) -> ZotaState | None:
        """Последнее состояние котла."""
        return self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        """Включён ли насос 2 (бит 1 маски ``[37]``).

        Реле при этом может продолжать работать и после выключения команды
        (постциркуляция/охлаждение) — фактическую работу реле показывает
        бинарный сенсор.
        """
        state = self._state
        if state is None:
            return None
        return state.pump2

    async def async_turn_on(self, **kwargs) -> None:
        """Включить насос 2 (cmd=0x0B, mode=0)."""
        await self._write(PUMP2_PAYLOAD_ON)

    async def async_turn_off(self, **kwargs) -> None:
        """Выключить насос 2 (cmd=0x0B, mode=1)."""
        await self._write(PUMP2_PAYLOAD_OFF)

    async def _write(self, payload: bytes) -> None:
        """Отправить payload записи и дождаться подтверждения котла."""
        try:
            result = await self.coordinator.async_send_raw(payload)
        except Exception as err:
            raise HomeAssistantError("котёл не подтвердил команду: %s" % err) from err
        if result != 0:
            raise HomeAssistantError("котёл не подтвердил команду")
        # Ускоренный опрос: обновлённое состояние нужно как можно скорее.
        await self.coordinator.async_poll_now()


__all__ = [
    "PUMP1_DESCRIPTION",
    "PUMP2_DESCRIPTION",
    "ZotaPump1Switch",
    "ZotaPump2Switch",
    "async_setup_entry",
]
