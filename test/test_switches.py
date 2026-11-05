#!/usr/bin/env python3
"""Standalone-тест платформы switch ZOTA Smart (насосы 1 и 2).

Запуск (без pytest, без установленного homeassistant):

    python3 test/test_switches.py

Home Assistant не установлен локально, поэтому модуль ``homeassistant``
подменяется минимальными заглушками ДО импорта платформ. Заглушки
вынесены в :mod:`ha_stubs` — общий харнесс для тестов интеграции.

Проверяется настоящий код платформы:

* насос 1: «вкл» → write(0x0A, 0x0101), «выкл» → write(0x0A, 0x0102);
  состояние — поле ``[29]`` info-блока (``[29]=1`` → включён);
* насос 2: «вкл» → send_raw(0b0000000000), «выкл» → send_raw(0b0001000000)
  с cmd_echo=0x0B; состояние — бит 1 маски ``[37]`` (``state.pump2``);
* после успешной команды — быстрый повторный опрос (cmd=0);
* неуспешный ACK (NAK) → HomeAssistantError.
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
from zota_smart.const import (  # noqa: E402
    CMD_PUMP1,
    CMD_PUMP2,
    PUMP2_PAYLOAD_OFF,
    PUMP2_PAYLOAD_ON,
)
from zota_smart.switch import (  # noqa: E402
    PUMP1_DESCRIPTION,
    PUMP2_DESCRIPTION,
    ZotaPump1Switch,
    ZotaPump2Switch,
)


class FakeTransport:
    """Транспорт-заглушка: принимает кадры и помнит отправленные записи."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent_frames: list[tuple[int, int, bytes]] = []
        self.writes: list[tuple[int, int]] = []
        #: Все payload'ы команд (длиной ≥ 4 байт) — как отправлены в сокет.
        self.raw_payloads: list[bytes] = []
        self.reads: list[int] = []
        #: Очередь ответов ACK: (cmd_echo, result). Пусто — запись не подтверждается.
        self.acks: list[tuple[int, int]] = []

    def send_frame(self, link: int, opcode: int, payload: bytes) -> bool:
        """Принять кадр от координатора."""
        self.sent_frames.append((link, opcode, payload))
        if len(payload) >= 4:
            cmd, value = struct.unpack_from("<HH", payload, 0)
            self.writes.append((cmd, value))
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
        """Остановить транспорт (не используется в тесте)."""


def make_coordinator(transport: FakeTransport):
    """Собрать координатор с фейковым транспортом."""
    from zota_smart.coordinator import ZotaCoordinator

    hass = FakeHass()
    entry = ConfigEntry(data={"name": "Котёл", "port": 1978})
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
) -> bytes:
    """Собрать info-блок (53 байта) с заданными полями.

    Раскладка ступеней — по живой валидации 2026-11-05 (веб ZOTA):
    ``[3]`` — текущая (активная) ступень, ``[4]`` — установленная (уставка).
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
    info[29] = pump1
    info[37] = mask
    info[46] = diag
    return bytes(info)


def set_state(coordinator, **fields) -> None:
    """Задать состояние котла."""
    from zota_smart.state import state_from_info

    coordinator._set_data(state_from_info(make_info(**fields)))


def make_pump1(coordinator):
    """Создать switch насоса 1."""
    return ZotaPump1Switch(coordinator, PUMP1_DESCRIPTION)


def make_pump2(coordinator):
    """Создать switch насоса 2."""
    return ZotaPump2Switch(coordinator, PUMP2_DESCRIPTION)


# --- Кейсы: насос 1 ----------------------------------------------------------


async def case_pump1_on() -> None:
    """Насос 1 «вкл» → write(cmd=0x0A, val=0x0101) — байты ``01 01``.

    Write-значение cmd=0x0A — u16 LE: ПЕРВЫЙ байт — режим, ВТОРОЙ —
    таймаут (``val = mode | (timeout << 8)``). Доказательство: боевой кадр
    облака ``0a000201`` — байты ``02 01`` (режим 2 первым).
    """
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    switch = make_pump1(coordinator)
    transport.acks.append((CMD_PUMP1, 0))
    await switch.async_turn_on()
    print(f"--- case 1: насос 1 вкл ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(0x0A, 0x0101)],
        f"ожидалось [(0x0A, 0x0101)], получено {transport.writes}",
    )
    check(
        transport.raw_payloads[0].hex() == "0a000101",
        f"байты payload: {transport.raw_payloads[0].hex()}",
    )
    print("  case 1: OK\n")


async def case_pump1_off() -> None:
    """Насос 1 «выкл» → write(cmd=0x0A, val=0x0102) — байты ``02 01``.

    Раньше кодировалось ``0x0201`` (байты ``01 02``) — это режим 1 = «вкл»,
    из-за чего выключить насос было невозможно.
    """
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    switch = make_pump1(coordinator)
    transport.acks.append((CMD_PUMP1, 0))
    await switch.async_turn_off()
    print(f"--- case 2: насос 1 выкл ---\n  writes = {transport.writes}")
    check(
        transport.writes == [(0x0A, 0x0102)],
        f"ожидалось [(0x0A, 0x0102)], получено {transport.writes}",
    )
    check(
        transport.raw_payloads[0].hex() == "0a000201",
        f"байты payload: {transport.raw_payloads[0].hex()}",
    )
    print("  case 2: OK\n")


async def case_pump1_state() -> None:
    """Состояние насоса 1 — по полю ``[29]`` info-блока."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    switch = make_pump1(coordinator)

    set_state(coordinator, pump1=1)
    value = switch.is_on
    print(f"--- case 3: состояние насоса 1 ---\n  [29]=1 → is_on = {value!r}")
    check(value is True, f"[29]=1: ожидалось True, получено {value!r}")

    set_state(coordinator, pump1=2)
    value = switch.is_on
    print(f"  [29]=2 → is_on = {value!r}")
    check(value is False, f"[29]=2: ожидалось False, получено {value!r}")

    # Значение, отличное от 1 и 2 (например, режим «Авто» котла) —
    # насос не считается принудительно включённым.
    set_state(coordinator, pump1=0)
    value = switch.is_on
    print(f"  [29]=0 → is_on = {value!r}")
    check(value is False, f"[29]=0: ожидалось False, получено {value!r}")
    print("  case 3: OK\n")


async def case_pump1_no_data() -> None:
    """Без данных — состояние None."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    switch = make_pump1(coordinator)
    value = switch.is_on
    print(f"--- case 4: нет данных ---\n  is_on = {value!r}")
    check(value is None, "ожидалось None без данных")
    print("  case 4: OK\n")


async def case_pump1_nak() -> None:
    """Неуспешный ACK → HomeAssistantError."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    switch = make_pump1(coordinator)
    transport.acks.append((CMD_PUMP1, 2))  # NAK
    try:
        await switch.async_turn_on()
    except HomeAssistantError as err:
        print(f"--- case 5: NAK ---\n  {err}")
        check("котёл не подтвердил команду" in str(err), f"текст ошибки: {err}")
    else:
        raise AssertionError("ожидался HomeAssistantError на NAK")
    print("  case 5: OK\n")


# --- Кейсы: насос 2 ----------------------------------------------------------


async def case_pump2_on() -> None:
    """Насос 2 «вкл» → send_raw(0b0000000000), cmd_echo=0x0B."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    switch = make_pump2(coordinator)
    transport.acks.append((CMD_PUMP2, 0))
    await switch.async_turn_on()
    print(f"--- case 6: насос 2 вкл ---\n  raw_payloads = {transport.raw_payloads}")
    check(
        transport.raw_payloads == [PUMP2_PAYLOAD_ON],
        f"ожидался payload {PUMP2_PAYLOAD_ON.hex()}, "
        f"получено {[p.hex() for p in transport.raw_payloads]}",
    )
    check(
        transport.raw_payloads[0].hex() == "0b0000000000",
        f"hex = {transport.raw_payloads[0].hex()}",
    )
    check(
        struct.unpack_from("<H", transport.raw_payloads[0], 0)[0] == 0x0B,
        "cmd_echo должен быть 0x0B",
    )
    print("  case 6: OK\n")


async def case_pump2_off() -> None:
    """Насос 2 «выкл» → send_raw(0b0001000000), cmd_echo=0x0B."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    switch = make_pump2(coordinator)
    transport.acks.append((CMD_PUMP2, 0))
    await switch.async_turn_off()
    print(f"--- case 7: насос 2 выкл ---\n  raw_payloads = {transport.raw_payloads}")
    check(
        transport.raw_payloads == [PUMP2_PAYLOAD_OFF],
        f"ожидался payload {PUMP2_PAYLOAD_OFF.hex()}, "
        f"получено {[p.hex() for p in transport.raw_payloads]}",
    )
    check(
        transport.raw_payloads[0].hex() == "0b0001000000",
        f"hex = {transport.raw_payloads[0].hex()}",
    )
    check(
        struct.unpack_from("<H", transport.raw_payloads[0], 0)[0] == 0x0B,
        "cmd_echo должен быть 0x0B",
    )
    print("  case 7: OK\n")


async def case_pump2_state() -> None:
    """Состояние насоса 2 — по биту 1 маски ``[37]``."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    switch = make_pump2(coordinator)

    set_state(coordinator, mask=0x11)  # бит 1 сброшен → насос 2 выключен
    value = switch.is_on
    print(f"--- case 8: состояние насоса 2 ---\n  mask=0x11 → is_on = {value!r}")
    check(value is False, f"ожидалось False, получено {value!r}")

    set_state(coordinator, mask=0x13)  # бит 1 установлен → насос 2 включён
    value = switch.is_on
    print(f"  mask=0x13 → is_on = {value!r}")
    check(value is True, f"ожидалось True, получено {value!r}")
    print("  case 8: OK\n")


async def case_pump2_nak() -> None:
    """Неуспешный ACK → HomeAssistantError."""
    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    set_state(coordinator)
    switch = make_pump2(coordinator)
    transport.acks.append((CMD_PUMP2, 2))  # NAK
    try:
        await switch.async_turn_on()
    except HomeAssistantError as err:
        print(f"--- case 9: NAK ---\n  {err}")
        check("котёл не подтвердил команду" in str(err), f"текст ошибки: {err}")
    else:
        raise AssertionError("ожидался HomeAssistantError на NAK")
    print("  case 9: OK\n")


# --- Кейсы: общее ------------------------------------------------------------


async def case_poll_now() -> None:
    """После успешной команды — быстрый повторный опрос (cmd=0)."""
    for name, switch_factory, ack_cmd in (
        ("насос 1", make_pump1, CMD_PUMP1),
        ("насос 2", make_pump2, CMD_PUMP2),
    ):
        transport = FakeTransport()
        coordinator = make_coordinator(transport)
        set_state(coordinator)
        switch = switch_factory(coordinator)
        transport.acks.append((ack_cmd, 0))
        await switch.async_turn_on()
        print(f"--- case 10: poll_now ({name}) ---\n  reads = {transport.reads}")
        check(0 in transport.reads, f"ожидался запрос cmd=0, получено {transport.reads}")
    print("  case 10: OK\n")


async def case_setup_entry() -> None:
    """В setup платформы создаются обе сущности — насос 1 и насос 2."""
    from zota_smart import switch as switch_module

    transport = FakeTransport()
    coordinator = make_coordinator(transport)
    hass = FakeHass()
    hass.data = {"zota_smart": {"test": coordinator}}
    added: list = []

    class _Entry:
        entry_id = "test"

    await switch_module.async_setup_entry(hass, _Entry(), added.extend)
    print(f"--- case 11: setup платформы ---\n  сущностей = {len(added)}")
    check(len(added) == 2, f"ожидалось 2 сущности, получено {len(added)}")
    keys = sorted(entity.entity_description.key for entity in added)
    print(f"  keys = {keys}")
    check(keys == ["pump1", "pump2"], f"keys: {keys}")
    uids = sorted(entity.unique_id for entity in added)
    print(f"  unique_id = {uids}")
    check(
        all(uid.endswith("_pump2") or uid.endswith("_pump1") for uid in uids),
        f"unique_id: {uids}",
    )
    print("  case 11: OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_switches: платформа switch (насосы 1 и 2) ===\n")

    async def run_all() -> None:
        await case_pump1_on()
        await case_pump1_off()
        await case_pump1_state()
        await case_pump1_no_data()
        await case_pump1_nak()
        await case_pump2_on()
        await case_pump2_off()
        await case_pump2_state()
        await case_pump2_nak()
        await case_poll_now()
        await case_setup_entry()

    asyncio.run(run_all())
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
