#!/usr/bin/env python3
"""Standalone-тест платформы climate ZOTA Smart.

Запуск (без pytest, без установленного homeassistant):

    python3 test/test_climate.py

Home Assistant не установлен локально, поэтому модуль ``homeassistant``
подменяется минимальными заглушками ДО импорта платформ. Заглушки
вынесены в :mod:`ha_stubs` — общий харнесс для тестов интеграции.

Проверяется настоящий код платформы:

* climate: пресет «10 кВт» → write(1, 2); уставка 55 → write(2, 55);
  hvac OFF → write(1, 0); HEAT → write(1, последняя ненулевая ступень);
  невалидные значения → HomeAssistantError; неуспешный ACK → ошибка.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
    ),
)

import ha_stubs  # noqa: E402,F401  — заглушки homeassistant ставятся до импорта

from ha_stubs import (  # noqa: E402
    ClimateEntityFeature,
    ConfigEntry,
    FakeHass,
    HomeAssistantError,
    HVACMode,
    check,
)
from zota_smart.climate import ZotaClimate  # noqa: E402


class FakeTransport:
    """Транспорт-заглушка: принимает кадры и помнит отправленные записи."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent_frames: list[tuple[int, int, bytes]] = []
        self.writes: list[tuple[int, int]] = []
        self.reads: list[int] = []
        #: Очередь ответов ACK: (cmd_echo, result). Пусто — write не подтверждается.
        self.acks: list[tuple[int, int]] = []

    def send_frame(self, link: int, opcode: int, payload: bytes) -> bool:
        """Принять кадр от координатора."""
        self.sent_frames.append((link, opcode, payload))
        import struct

        if len(payload) >= 4:
            cmd, value = struct.unpack_from("<HH", payload, 0)
            self.writes.append((cmd, value))
            for cmd_echo, result in list(self.acks):
                if cmd_echo == cmd:
                    self.acks.remove((cmd_echo, result))
                    self.coordinator._handle_ack(
                        struct.pack("<HH", cmd_echo, result)
                    )
                    break
        elif len(payload) >= 2:
            import struct

            (cmd,) = struct.unpack_from("<H", payload, 0)
            self.reads.append(cmd)
        return True

    def stop(self) -> None:
        """Остановить транспорт (не используется в тесте)."""


def make_coordinator(transport: FakeTransport, entry_data=None):
    """Собрать координатор с фейковым транспортом."""
    from zota_smart.coordinator import ZotaCoordinator

    hass = FakeHass()
    if entry_data is None:
        entry_data = {"name": "Котёл", "port": 1978}
    entry = ConfigEntry(data=entry_data)
    coordinator = ZotaCoordinator(hass, entry, transport=transport)
    transport.coordinator = coordinator
    return coordinator


def make_info(
    stage=1,
    active_stage=None,
    setpoint=61,
    t_return=20,
    t_supply=21,
    pump1=1,
    mask=0x11,
    diag=180,
    sensor_room=25,
    sensor_outside=7,
) -> bytes:
    """Собрать info-блок (53 байта) с заданными полями.

    Раскладка ступеней — по живой валидации 2026-11-05 (веб ZOTA):
    ``[3]`` — **текущая (активная)** ступень, ``[4]`` — **установленная**
    (уставка). Поэтому ``stage`` (уставка) кладётся в ``[4]``, а
    ``active_stage`` (текущая) — в ``[3]``.

    ``active_stage`` по умолчанию равен ``stage``; задаётся отдельно, чтобы
    проверить признак «Нагрев».
    """
    from zota_smart.state import INFO_SIZE

    if active_stage is None:
        active_stage = stage
    info = bytearray(INFO_SIZE)
    info[3] = active_stage
    info[4] = stage
    info[5] = setpoint
    info[15] = t_return
    info[21] = t_supply
    #: Слоты внешних датчиков u16 LE (``None`` → маркер «не подключён»).
    import struct as _struct

    info[17:19] = _struct.pack("<H", 0x4001 if sensor_room is None else int(sensor_room))
    info[19:21] = _struct.pack(
        "<H", 0x4001 if sensor_outside is None else int(sensor_outside)
    )
    info[29] = pump1
    info[37] = mask
    info[46] = diag
    return bytes(info)


def set_state(coordinator, **fields) -> None:
    """Задать состояние котла."""
    from zota_smart.state import state_from_info

    coordinator._set_data(state_from_info(make_info(**fields)))


def make_climate(coordinator):
    """Создать климатическую сущность."""
    from zota_smart.climate import CLIMATE_DESCRIPTION

    return ZotaClimate(coordinator, CLIMATE_DESCRIPTION)


def write_case(transport: FakeTransport, cmd: int, value: int) -> None:
    """Подтвердить запись cmd ACK'ом result=0."""
    transport.acks.append((cmd, 0))


# --- Кейсы climate -----------------------------------------------------------


async def case_climate_preset() -> None:
    """Пресет «10 кВт» → запись cmd=1, val=2."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator, stage=2)
    climate = make_climate(coordinator)
    write_case(transport, 1, 2)
    await climate.async_set_preset_mode("10 кВт")
    print(f"--- case 1: пресет «10 кВт» ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(1, 2)],
        f"writes: ожидалось [(1, 2)], получено {transport.writes}",
    )
    check(climate.preset_mode == "10 кВт", f"preset_mode: {climate.preset_mode!r}")
    print("  case 1: OK\n")


async def case_climate_setpoint() -> None:
    """Уставка 55 → запись cmd=2, val=55."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    climate = make_climate(coordinator)
    write_case(transport, 2, 55)
    await climate.async_set_temperature(temperature=55)
    print(f"--- case 2: уставка 55 ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(2, 55)],
        f"writes: ожидалось [(2, 55)], получено {transport.writes}",
    )
    print("  case 2: OK\n")


async def case_climate_hvac_off() -> None:
    """Режим OFF → запись cmd=1, val=0."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator, stage=2)
    climate = make_climate(coordinator)
    write_case(transport, 1, 0)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    print(f"--- case 3: hvac OFF ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(1, 0)],
        f"writes: ожидалось [(1, 0)], получено {transport.writes}",
    )
    print("  case 3: OK\n")


async def case_climate_hvac_heat() -> None:
    """Режим HEAT → запись cmd=1 с последней ненулевой ступенью."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator, stage=3)
    climate = make_climate(coordinator)
    write_case(transport, 1, 3)
    await climate.async_set_preset_mode("15 кВт")
    transport.writes.clear()
    write_case(transport, 1, 3)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    transport.writes.clear()
    write_case(transport, 1, 3)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    print(f"--- case 4: hvac HEAT после OFF ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(1, 3)],
        f"writes: ожидалось [(1, 3)], получено {transport.writes}",
    )
    print("  case 4: OK\n")


async def case_climate_heat_default() -> None:
    """HEAT без предыдущей записи → ступень 1."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator, stage=0)
    climate = make_climate(coordinator)
    write_case(transport, 1, 1)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    print(f"--- case 5: HEAT по умолчанию ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(1, 1)],
        f"writes: ожидалось [(1, 1)], получено {transport.writes}",
    )
    print("  case 5: OK\n")


async def case_climate_states() -> None:
    """Текущие значения климата из состояния котла."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator, stage=2, setpoint=61, t_supply=21)
    climate = make_climate(coordinator)
    print(
        f"--- case 6: состояние климата ---\n"
        f"  current_temperature = {climate.current_temperature}\n"
        f"  target_temperature = {climate.target_temperature}\n"
        f"  hvac_mode           = {climate.hvac_mode}\n"
        f"  preset_mode         = {climate.preset_mode}"
    )
    check(climate.current_temperature == 21, "current_temperature")
    check(climate.target_temperature == 61, "target_temperature")
    check(climate.hvac_mode == HVACMode.HEAT, "hvac_mode")
    check(climate.preset_mode == "10 кВт", "preset_mode")
    # Нулевая ступень → OFF
    set_state(coordinator, stage=0)
    check(climate.hvac_mode == HVACMode.OFF, "hvac_mode при stage=0")
    check(climate.preset_mode == "Выкл", "preset_mode при stage=0")
    # Диапазоны и режимы
    check(climate.min_temp == 30, f"min_temp: {climate.min_temp}")
    check(climate.max_temp == 90, f"max_temp: {climate.max_temp}")
    check(climate.target_temperature_step == 1, "target_temperature_step")
    check(
        list(climate.hvac_modes) == [HVACMode.OFF, HVACMode.HEAT],
        f"hvac_modes: {climate.hvac_modes}",
    )
    check(
        list(climate.preset_modes) == ["Выкл", "5 кВт", "10 кВт", "15 кВт"],
        f"preset_modes: {climate.preset_modes}",
    )
    check(climate.temperature_unit == "°C", f"temperature_unit: {climate.temperature_unit}")
    check(
        climate.supported_features
        == ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE,
        f"supported_features: {climate.supported_features}",
    )
    print("  case 6: OK\n")


async def case_climate_invalid() -> None:
    """Невалидные значения отклоняются без записи."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    climate = make_climate(coordinator)
    for kwargs, message in (
        ({"temperature": 20}, "уставка ниже минимума"),
        ({"temperature": 120}, "уставка выше максимума"),
    ):
        try:
            await climate.async_set_temperature(**kwargs)
        except HomeAssistantError:
            pass
        else:
            raise AssertionError(f"ожидался HomeAssistantError: {message}")
    try:
        await climate.async_set_preset_mode("20 кВт")
    except HomeAssistantError:
        pass
    else:
        raise AssertionError("ожидался HomeAssistantError на неизвестный пресет")
    print(f"--- case 7: невалидные значения ---\n  writes = {transport.writes}")
    check(transport.writes == [], f"записей быть не должно: {transport.writes}")
    print("  case 7: OK\n")


async def case_climate_nak() -> None:
    """Неуспешный ACK → HomeAssistantError «котёл не подтвердил команду»."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    climate = make_climate(coordinator)
    transport.acks.append((1, 2))  # NAK
    try:
        await climate.async_set_preset_mode("5 кВт")
    except HomeAssistantError as err:
        print(f"--- case 8: NAK ---\n  {err}")
        check(
            "котёл не подтвердил команду" in str(err),
            f"текст ошибки: {err}",
        )
    else:
        raise AssertionError("ожидался HomeAssistantError на NAK")
    print("  case 8: OK\n")


async def case_climate_poll_now() -> None:
    """После успешной записи — быстрый повторный опрос (cmd=0)."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    climate = make_climate(coordinator)
    write_case(transport, 2, 55)
    await climate.async_set_temperature(temperature=55)
    print(f"--- case 9: poll_now ---\n  reads = {transport.reads}")
    check(0 in transport.reads, f"ожидался запрос cmd=0, получено {transport.reads}")
    print("  case 9: OK\n")


async def case_climate_power_18() -> None:
    """Котёл 18 кВт: пресеты Выкл/6/12/18, пресет «12 кВт» → write(1, 2)."""
    transport = FakeTransport()
    coordinator = make_coordinator(
        transport, {"name": "Котёл", "port": 1978, "power_kw": 18}
    )
    set_state(coordinator, stage=2)
    climate = make_climate(coordinator)
    print(
        f"--- case 10: пресеты для 18 кВт ---\n"
        f"  preset_modes = {climate.preset_modes}"
    )
    check(
        list(climate.preset_modes) == ["Выкл", "6 кВт", "12 кВт", "18 кВт"],
        f"preset_modes (18 кВт): {climate.preset_modes}",
    )
    check(climate.preset_mode == "12 кВт", f"preset_mode: {climate.preset_mode!r}")
    write_case(transport, 1, 2)
    await climate.async_set_preset_mode("12 кВт")
    check(
        transport.writes == [(1, 2)],
        f"writes: ожидалось [(1, 2)], получено {transport.writes}",
    )
    # Пресет «10 кВт» (15-кВт котла) больше не существует.
    try:
        await climate.async_set_preset_mode("10 кВт")
    except HomeAssistantError:
        pass
    else:
        raise AssertionError("ожидался HomeAssistantError на чужой пресет")
    print("  case 10: OK\n")


async def case_config_psk_power() -> None:
    """PSK и мощность берутся из entry, при отсутствии — дефолты."""
    from zota_smart.const import DEFAULT_PSK, build_stage_presets

    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    print(
        f"--- case 11: PSK/мощность из entry ---\n"
        f"  psk (фолбэк) = {coordinator.psk_hex[:6]}…\n"
        f"  power_kw     = {coordinator.power_kw}"
    )
    check(coordinator.psk_hex == DEFAULT_PSK, "psk_hex: ожидался фолбэк DEFAULT_PSK")
    check(coordinator.power_kw == 15, f"power_kw: {coordinator.power_kw}")
    check(
        build_stage_presets(15) == ("Выкл", "5 кВт", "10 кВт", "15 кВт"),
        f"пресеты 15 кВт: {build_stage_presets(15)}",
    )
    check(
        build_stage_presets(18) == ("Выкл", "6 кВт", "12 кВт", "18 кВт"),
        f"пресеты 18 кВт: {build_stage_presets(18)}",
    )
    own_psk = "ab" * 32
    transport2 = FakeTransport()
    coordinator2 = make_coordinator(
        transport2,
        {"name": "Котёл", "port": 1978, "psk": own_psk, "power_kw": 21},
    )
    check(coordinator2.psk_hex == own_psk, "psk_hex: ожидался PSK из entry")
    check(coordinator2.power_kw == 21, f"power_kw: {coordinator2.power_kw}")
    check(
        build_stage_presets(21) == ("Выкл", "7 кВт", "14 кВт", "21 кВт"),
        f"пресеты 21 кВт: {build_stage_presets(21)}",
    )
    print("  case 11: OK\n")


async def case_temp_sensor_source() -> None:
    """Источник current_temperature — настройка temp_sensor записи.

    Дефолт (поле отсутствует) — подача, поведение не меняется; ``room`` —
    слот [17..18]; неподключённый датчик (0x4001) → None.
    """
    # Дефолт: поле в entry отсутствует → подача.
    transport = FakeTransport()
    coordinator = make_coordinator(transport)  # без temp_sensor в entry
    set_state(coordinator, t_supply=21, sensor_room=25)
    climate = make_climate(coordinator)
    print(
        f"--- case 17: источник температуры ---\n"
        f"  default  = {climate.current_temperature}"
    )
    check(climate.current_temperature == 21, "дефолт должен быть подача (21)")

    # room → слот [17..18].
    transport2 = FakeTransport()
    coordinator2 = make_coordinator(
        transport2, {"name": "Котёл", "port": 1978, "temp_sensor": "room"}
    )
    set_state(coordinator2, t_supply=21, sensor_room=25)
    climate2 = make_climate(coordinator2)
    print(f"  room     = {climate2.current_temperature}")
    check(climate2.current_temperature == 25, "room должен читать [17..18] (25)")

    # return → поле [15].
    transport3 = FakeTransport()
    coordinator3 = make_coordinator(
        transport3, {"name": "Котёл", "port": 1978, "temp_sensor": "return"}
    )
    set_state(coordinator3, t_return=33)
    climate3 = make_climate(coordinator3)
    check(climate3.current_temperature == 33, "return должен читать [15] (33)")

    # outside → слот [19..20].
    transport4 = FakeTransport()
    coordinator4 = make_coordinator(
        transport4, {"name": "Котёл", "port": 1978, "temp_sensor": "outside"}
    )
    set_state(coordinator4, sensor_outside=7)
    climate4 = make_climate(coordinator4)
    check(climate4.current_temperature == 7, "outside должен читать [19..20] (7)")

    # Датчик не подключён (0x4001) → None, climate показывает «нет данных».
    transport5 = FakeTransport()
    coordinator5 = make_coordinator(
        transport5, {"name": "Котёл", "port": 1978, "temp_sensor": "room"}
    )
    set_state(coordinator5, sensor_room=None)
    climate5 = make_climate(coordinator5)
    print(f"  absent   = {climate5.current_temperature}")
    check(climate5.current_temperature is None, "0x4001 → None")
    print("  case 17: OK\n")


# --- Прочее ------------------------------------------------------------------


def case_manifest() -> None:
    """В манифесте нет requirements."""
    import json

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "zota_smart",
        "manifest.json",
    )
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    print(f"--- case 16: манифест ---\n  requirements = {manifest['requirements']!r}")
    check(manifest["requirements"] == [], f"requirements: {manifest['requirements']!r}")
    print("  case 16: OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_climate: платформа climate ===\n")

    async def run_all() -> None:
        await case_climate_preset()
        await case_climate_setpoint()
        await case_climate_hvac_off()
        await case_climate_hvac_heat()
        await case_climate_heat_default()
        await case_climate_states()
        await case_climate_invalid()
        await case_climate_nak()
        await case_climate_poll_now()
        await case_climate_power_18()
        await case_config_psk_power()
        await case_temp_sensor_source()

    asyncio.run(run_all())
    case_manifest()
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
