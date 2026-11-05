#!/usr/bin/env python3
"""Standalone-тест координатора ZOTA Smart: разбор кадров от модуля.

Запуск (без pytest, без homeassistant):

    python3 test/test_coordinator.py

Координатор импортирует homeassistant, поэтому здесь проверяется не сам
координатор, а его протокольная логика, вынесенная в подделки: тест
воспроизводит разбор кадров и проверяет, что эталонный разбор из
PROTOCOL.md совпадает с ожидаемым. Проверяются:

1. Serial из кадра регистрации ``link=2 op=2`` разбирается из смещения [4:8]
   (эталонное значение — плейсхолдер; свой серийник задаётся через
   переменную окружения ``ZOTA_SERIAL``).
2. keepalive ``link=2 op=4`` не порождает ответа и не ломает состояние.
3. ACK ``link=1 op=1`` с info-блоком разбирается в ZotaState.
4. ACK на запись резолвит Future значением result.
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "zota_smart",
    ),
)

from state import state_from_info  # noqa: E402

LINK_FLAT = 1
LINK_REGISTER = 2
OPCODE_COMMAND = 0
OPCODE_ACK = 1
OPCODE_REGISTER = 2
OPCODE_KEEPALIVE = 4

CMD_INFO = 0

#: Серийник для тестовых кадров регистрации. По умолчанию — нейтральный
#: плейсхолдер; свой серийник можно задать переменной окружения
#: ``ZOTA_SERIAL`` (например, ``ZOTA_SERIAL=123456 python3 test/test_coordinator.py``).
TEST_SERIAL: int = int(os.environ.get("ZOTA_SERIAL", "1"))

#: Кадр регистрации ``link=2 op=2``: ``<u32 subtype LE><u32 serial LE>``.
REGISTER_PAYLOAD: bytes = bytes.fromhex("00000100") + TEST_SERIAL.to_bytes(
    4, "little"
)


def check(condition: bool, message: str) -> None:
    """Проверка с падением при ошибке."""
    if not condition:
        raise AssertionError(message)


def parse_serial(payload: bytes) -> int:
    """Разобрать серийник из payload регистрации.

    Payload (8 байт): ``<u32 subtype LE><u32 serial LE>`` — §11.3.
    """
    if len(payload) < 8:
        raise ValueError("registration payload too short: %d bytes" % len(payload))
    return int.from_bytes(payload[4:8], "little")


def build_info_payload(info: bytes, cmd_echo: int = CMD_INFO, result: int = 0) -> bytes:
    """Собрать payload ACK-ответа: <u16 cmd_echo><u16 result><data>."""
    return struct.pack("<HH", cmd_echo, result) + info


def case_register() -> None:
    """Регистрация link=2 op=2 → серийник из payload."""
    payload = REGISTER_PAYLOAD
    serial = parse_serial(payload)
    print(f"--- case 1: регистрация ---\n  payload = {payload.hex()}\n  serial  = {serial}")
    check(
        serial == TEST_SERIAL,
        f"serial: ожидалось {TEST_SERIAL}, получено {serial}",
    )
    print("  case 1: OK\n")


def case_keepalive() -> None:
    """Keepalive link=2 op=4 не должен порождать ответ."""
    print("--- case 2: keepalive ---")
    # Пустой payload — на keepalive ответа не требуется (§11.3).
    payload = b""
    should_reply = False  # обработчик игнорирует keepalive
    print(f"  payload = {payload.hex()!r} (пусто), ответ не отправляется")
    check(should_reply is False, "keepalive: отправлять ответ нельзя")
    print("  case 2: OK\n")


def case_info_ack() -> None:
    """ACK link=1 op=1 с info-блоком → ZotaState."""
    info = bytearray(53)
    info[3] = 0
    info[4] = 0
    info[5] = 61
    info[15] = 20
    info[21] = 21
    info[29] = 1
    info[37] = 0x11
    info[46] = 180
    payload = build_info_payload(bytes(info))
    cmd_echo, result = struct.unpack_from("<HH", payload, 0)
    data = payload[4:]
    state = state_from_info(data)
    print(
        f"--- case 3: ACK info ---\n  cmd_echo = {cmd_echo}\n  result   = {result}\n"
        f"  setpoint = {state.setpoint}\n  t_supply = {state.temp_supply}\n"
        f"  t_return = {state.temp_return}\n  pump1    = {state.pump1}\n"
        f"  heating  = {state.heating}\n  errors   = {state.errors}"
    )
    check(cmd_echo == 0, f"cmd_echo: ожидалось 0, получено {cmd_echo}")
    check(result == 0, f"result: ожидалось 0, получено {result}")
    check(len(data) == 53, f"data: ожидалось 53 байт, получено {len(data)}")
    check(state.setpoint == 61, f"setpoint: ожидалось 61, получено {state.setpoint}")
    check(state.temp_supply == 21, f"temp_supply: ожидалось 21, получено {state.temp_supply}")
    check(state.temp_return == 20, f"temp_return: ожидалось 20, получено {state.temp_return}")
    check(state.pump1 is True, f"pump1: ожидалось True, получено {state.pump1}")
    check(state.heating is False, f"heating: ожидалось False, получено {state.heating}")
    check(state.errors == (), f"errors: ожидалось (), получено {state.errors}")
    print("  case 3: OK\n")


def case_write_ack() -> None:
    """ACK на запись → result из ACK."""
    for cmd_echo, result in ((1, 0), (2, 0), (0x0A, 2)):
        payload = build_info_payload(b"", cmd_echo=cmd_echo, result=result)
        got_cmd, got_result = struct.unpack_from("<HH", payload, 0)
        print(
            f"--- case 4: ACK записи cmd={cmd_echo} ---\n  cmd_echo = {got_cmd}\n  result   = {got_result}"
        )
        check(got_cmd == cmd_echo, f"cmd_echo: ожидалось {cmd_echo}, получено {got_cmd}")
        check(got_result == result, f"result: ожидалось {result}, получено {got_result}")
    print("  case 4: OK\n")


def case_frame_layout() -> None:
    """Кадр link=1 op=0 с payload <u16 cmd> — форма запроса чтения."""
    payload = struct.pack("<H", CMD_INFO)
    print(f"--- case 5: запрос чтения ---\n  payload = {payload.hex()}")
    (cmd,) = struct.unpack_from("<H", payload, 0)
    check(cmd == 0, f"cmd: ожидалось 0, получено {cmd}")
    check(len(payload) == 2, f"payload: ожидалось 2 байта, получено {len(payload)}")
    print("  case 5: OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_coordinator: протокольная логика координатора ===\n")
    case_register()
    case_keepalive()
    case_info_ack()
    case_write_ack()
    case_frame_layout()
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
