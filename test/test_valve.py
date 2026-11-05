#!/usr/bin/env python3
"""Standalone-тест клапана ZOTA Smart (cmd=0x0C) и бага-фикса ошибок.

Запуск (без pytest, без homeassistant):

    python3 test/test_valve.py

Проверяется по живому захвату 2026-11-06 (PROTOCOL.md §11.13):

* сборка 11-байтового write-payload клапана (итоговое состояние опыта:
  режим ЦО, t° 79, включение 19 с, периодичность 190 с, полное открытие
  290 с — hex кадра из лога);
* парсинг valve-полей из info-блока с теми же значениями;
* бага-фикс: ошибки = ``data[48..52]``; байт ``[47]`` (старший байт полного
  открытия) ошибкой не является;
* координатор: ``send_valve_config(mode=0)`` пишет полный блок с текущими
  значениями остальных полей, cmd_echo=0x0C, после ACK — poll_now.
"""

from __future__ import annotations

import asyncio
import os
import struct
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
    ConfigEntry,
    FakeHass,
    HomeAssistantError,
    check,
)
from zota_smart.const import CMD_VALVE  # noqa: E402
from zota_smart.coordinator import build_valve_payload  # noqa: E402
from zota_smart.state import INFO_SIZE, state_from_info  # noqa: E402


def make_info(**fields: int) -> bytes:
    """Собрать info-блок (53 байта); ключи — индексы байт."""
    blob = bytearray(INFO_SIZE)
    for index, value in fields.items():
        blob[int(index.lstrip("_"))] = value
    return bytes(blob)


def case_payload() -> None:
    """Payload клапана по живому захвату: 11 байт, hex из лога §11.13.3."""
    payload = build_valve_payload(
        mode=2, target=79, on_time=19, period=190, full_open=290
    )
    hexstr = payload.hex()
    print(f"--- case 1: payload клапана ---\n  len = {len(payload)}, hex = {hexstr}")
    check(len(payload) == 11, f"длина: ожидалось 11, получено {len(payload)}")
    # §11.13.3: 0c00 | 02 | 01 | 4f | 19 | 13 | be00 | 2201
    check(
        hexstr == "0c0002014f1913be002201",
        f"ожидался 0c0002014f1913be002201, получено {hexstr}",
    )
    check(struct.unpack_from("<H", payload, 0)[0] == CMD_VALVE, "cmd должен быть 0x0C")
    check(payload[2] == 0x02, "байт [2] — режим")
    check(payload[3] == 0x01, "байт [3] — константа 01")
    check(payload[4] == 0x4F, "байт [4] — целевая 79")
    check(payload[5] == 0x19, "байт [5] — константа 0x19")
    check(payload[6] == 0x13, "байт [6] — включение 19")
    check(struct.unpack_from("<H", payload, 7)[0] == 190, "u16le [7..8] — 190")
    check(struct.unpack_from("<H", payload, 9)[0] == 290, "u16le [9..10] — 290")
    print("  case 1: OK\n")


def case_parse() -> None:
    """Парсинг valve-полей info-блока (итоговое состояние опыта §11.13.3)."""
    blob = make_info(
        _7=79,           # целевая t° клапана
        _41=2,           # режим ЦО
        _42=1,           # константа 01
        _43=19,          # включение 19 с (0x13)
        _44=0xBE, _45=0,    # периодичность 190
        _46=0x22, _47=0x01,  # полное открытие 290
    )
    state = state_from_info(blob)
    print(
        f"--- case 2: парсинг valve-полей ---\n"
        f"  mode={state.valve_mode} target={state.valve_target} "
        f"on_time={state.valve_on_time} period={state.valve_period} "
        f"full_open={state.valve_full_open}"
    )
    check(state.valve_mode == 2, f"valve_mode: ожидалось 2, получено {state.valve_mode}")
    check(state.valve_target == 79, f"valve_target: ожидалось 79, получено {state.valve_target}")
    check(state.valve_on_time == 19, f"valve_on_time: ожидалось 19, получено {state.valve_on_time}")
    check(state.valve_period == 190, f"valve_period: ожидалось 190, получено {state.valve_period}")
    check(state.valve_full_open == 290, f"valve_full_open: ожидалось 290, получено {state.valve_full_open}")
    print("  case 2: OK\n")


def case_errors_offset() -> None:
    """Бага-фикс: ошибки — data[48..52]; [47] принадлежит полному открытию."""
    blob = make_info(
        _46=0x22, _47=0x01,  # полное открытие 290 — [47] ≠ ошибка
        _48=3, _50=1,
    )
    state = state_from_info(blob)
    print(
        f"--- case 3: ошибки [48..52] ---\n"
        f"  errors = {state.errors}, offsets = {state.error_offsets}, "
        f"summary = {state.errors_summary}"
    )
    check(state.errors == (3, 1), f"errors: ожидалось (3, 1), получено {state.errors}")
    check(state.error_offsets == (48, 50), f"offsets: {state.error_offsets}")
    check(state.errors_summary == "E48=3, E50=1", f"summary: {state.errors_summary}")
    check(state.valve_full_open == 290, "полное открытие не должно попадать в ошибки")

    # Ошибка в [52] — последний слот, тоже ловится.
    blob = make_info(_52=7)
    state = state_from_info(blob)
    check(state.errors == (7,) and state.error_offsets == (52,), "слот [52] — ошибка")
    print("  case 3: OK\n")


class FakeTransport:
    """Транспорт-заглушка (как в test_switches)."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent_frames: list[tuple[int, int, bytes]] = []
        self.raw_payloads: list[bytes] = []
        self.reads: list[int] = []
        self.acks: list[tuple[int, int]] = []

    def send_frame(self, link: int, opcode: int, payload: bytes) -> bool:
        self.sent_frames.append((link, opcode, payload))
        if len(payload) >= 4:
            (cmd,) = struct.unpack_from("<H", payload, 0)
            self.raw_payloads.append(bytes(payload))
            for cmd_echo, result in list(self.acks):
                if cmd_echo == cmd:
                    self.acks.remove((cmd_echo, result))
                    self.coordinator._handle_ack(
                        struct.pack("<HH", cmd_echo, result)
                    )
                    break
        elif len(payload) >= 2:
            (cmd,) = struct.unpack_from("<H", payload, 0)
            self.reads.append(cmd)
        return True

    def stop(self) -> None:
        """Не используется в тесте."""


async def case_send_valve_config() -> None:
    """send_valve_config(mode=0) — полный блок с текущими остальными полями."""
    from zota_smart.coordinator import ZotaCoordinator

    transport = FakeTransport()
    hass = FakeHass()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
    coordinator = ZotaCoordinator(hass, entry, transport=transport)
    transport.coordinator = coordinator
    coordinator._set_data(
        state_from_info(
            make_info(_7=79, _41=2, _43=19, _44=0xBE, _45=0, _46=0x22, _47=0x01)
        )
    )
    transport.acks.append((CMD_VALVE, 0))
    result = await asyncio.wrap_future(coordinator.send_valve_config(mode=0))
    hexstr = transport.raw_payloads[0].hex()
    print(f"--- case 4: send_valve_config ---\n  result = {result}, payload = {hexstr}")
    check(result == 0, f"ACK result: ожидался 0, получено {result}")
    check(
        hexstr == "0c0000014f1913be002201",
        f"режим 0 при сохранении остальных полей: {hexstr}",
    )
    check(transport.reads == [], "сам координатор опрос не триггерит (делает сущность)")

    # Валидация диапазонов.
    try:
        coordinator.send_valve_config(target=10)
    except ValueError as err:
        print(f"  target=10 → ValueError: {err}")
    else:
        raise AssertionError("ожидался ValueError на target вне диапазона")
    print("  case 4: OK\n")


async def case_platforms() -> None:
    """Select и number: импорт, опции, запись через send_valve_config."""
    from zota_smart.coordinator import ZotaCoordinator
    from zota_smart.number import VALVE_NUMBERS, ZotaValveNumber
    from zota_smart.select import VALVE_MODE_DESCRIPTION, ZotaValveModeSelect

    transport = FakeTransport()
    hass = FakeHass()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
    coordinator = ZotaCoordinator(hass, entry, transport=transport)
    transport.coordinator = coordinator
    coordinator._set_data(
        state_from_info(
            make_info(_7=79, _41=2, _43=19, _44=0xBE, _45=0, _46=0x22, _47=0x01)
        )
    )

    select = ZotaValveModeSelect(coordinator, VALVE_MODE_DESCRIPTION)
    check(select.options == ["Выключен", "Тёплый пол", "ЦО"], f"опции: {select.options}")
    check(select.current_option == "ЦО", f"current: {select.current_option}")
    transport.acks.append((CMD_VALVE, 0))
    await select.async_select_option("Выключен")
    check(
        transport.raw_payloads[-1].hex() == "0c0000014f1913be002201",
        f"payload: {transport.raw_payloads[-1].hex()}",
    )
    check(0 in transport.reads, "после записи — быстрый poll (cmd=0)")

    descriptions = {d.key: d for d in VALVE_NUMBERS}
    check(
        sorted(descriptions) == ["valve_full_open", "valve_on_time", "valve_period", "valve_target"],
        f"keys: {sorted(descriptions)}",
    )
    target = ZotaValveNumber(coordinator, descriptions["valve_target"])
    check(target.native_value == 79.0, f"native_value: {target.native_value}")
    transport.acks.append((CMD_VALVE, 0))
    await target.async_set_native_value(50)
    check(
        transport.raw_payloads[-1].hex() == "0c000201321913be002201",
        f"payload target=50: {transport.raw_payloads[-1].hex()}",
    )

    try:
        await target.async_set_native_value(10)
    except HomeAssistantError:
        print("  target=10 → HomeAssistantError (диапазон)")
    else:
        raise AssertionError("ожидался HomeAssistantError на значение вне диапазона")
    print("  case 5: OK\n")


async def case_floor() -> None:
    """Двойная целевая и защита Тёплого пола (§11.13.6)."""
    from zota_smart.coordinator import ZotaCoordinator
    from zota_smart.number import VALVE_NUMBERS, ZotaValveNumber
    from zota_smart.select import VALVE_MODE_DESCRIPTION, ZotaValveModeSelect

    transport = FakeTransport()
    hass = FakeHass()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
    coordinator = ZotaCoordinator(hass, entry, transport=transport)
    transport.coordinator = coordinator
    coordinator._set_data(
        state_from_info(
            make_info(_7=79, _10=32, _41=2, _43=19, _44=0xBE, _45=0, _46=0x22, _47=0x01)
        )
    )

    select = ZotaValveModeSelect(coordinator, VALVE_MODE_DESCRIPTION)
    # (а) select → Тёплый пол шлёт mode=1 с target=data[10]=32.
    transport.acks.append((CMD_VALVE, 0))
    await select.async_select_option("Тёплый пол")
    check(
        transport.raw_payloads[-1].hex() == "0c000101201913be002201",
        f"payload floor: {transport.raw_payloads[-1].hex()}",
    )
    # (б) select → ЦО шлёт mode=2 с target=data[7]=79.
    transport.acks.append((CMD_VALVE, 0))
    await select.async_select_option("ЦО")
    check(
        transport.raw_payloads[-1].hex() == "0c0002014f1913be002201",
        f"payload heating: {transport.raw_payloads[-1].hex()}",
    )

    # (в) number в режиме Тёплый пол: value=[10]=32, max=50; в ЦО max=90.
    descriptions = {d.key: d for d in VALVE_NUMBERS}
    target = ZotaValveNumber(coordinator, descriptions["valve_target"])
    coordinator._set_data(
        state_from_info(
            make_info(_7=79, _10=32, _41=1, _43=19, _44=0xBE, _45=0, _46=0x22, _47=0x01)
        )
    )
    check(select.current_option == "Тёплый пол", f"current: {select.current_option}")
    check(target.native_value == 32.0, f"native_value floor: {target.native_value}")
    check(target.native_max_value == 50.0, f"max floor: {target.native_max_value}")
    coordinator._set_data(
        state_from_info(
            make_info(_7=79, _10=32, _41=2, _43=19, _44=0xBE, _45=0, _46=0x22, _47=0x01)
        )
    )
    check(target.native_value == 79.0, f"native_value heating: {target.native_value}")
    check(target.native_max_value == 90.0, f"max heating: {target.native_max_value}")
    for desc in VALVE_NUMBERS:
        entity = ZotaValveNumber(coordinator, desc)
        check(
            entity.mode == "slider",
            f"mode {desc.key}: {entity.mode}",
        )

    # (г) target=60 при активном Тёплом поле — исключение без отправки.
    coordinator._set_data(
        state_from_info(
            make_info(_7=79, _10=32, _41=1, _43=19, _44=0xBE, _45=0, _46=0x22, _47=0x01)
        )
    )
    before = len(transport.raw_payloads)
    try:
        coordinator.send_valve_config(target=60)
    except ValueError as err:
        print(f"  target=60 в Тёплом поле → ValueError: {err}")
    else:
        raise AssertionError("ожидался ValueError на target=60 в Тёплом поле")
    check(
        len(transport.raw_payloads) == before,
        "кадр не должен отправляться при отказе валидации",
    )
    print("  case 6: OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_valve: клапан cmd=0x0C + бага-фикс ошибок ===\n")

    async def run_all() -> None:
        await case_send_valve_config()
        await case_platforms()
        await case_floor()

    case_payload()
    case_parse()
    case_errors_offset()
    asyncio.run(run_all())
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
