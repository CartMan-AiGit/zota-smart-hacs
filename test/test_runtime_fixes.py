#!/usr/bin/env python3
"""Standalone-тесты runtime-фиксов f22 (гонка записей, power_kw, лог кадров).

Запуск (без pytest, без homeassistant):

    python3 test/test_runtime_fixes.py

Проверяется:

1. Две последовательные/конкурентные записи с одним cmd: сериализация
   ``_write_serial`` не теряет первую команду — обе получают свой ACK,
   вечного hang нет (``asyncio.wait_for`` с запасным таймаутом).
2. ``power_kw=18`` → ``kw_per_stage=6``: «Мощность» по ступеням 1/2/3
   даёт 6/12/18 кВт; подписи ступеней динамические («6 кВт»). Дефолт
   (15 кВт) не меняется: ступень 1 → 5 кВт.
3. Опция ``frame_log`` выключена → ``log_frame`` не пишет файл; включена —
   пишет; при превышении порога лог ротируется в ``.old``.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile

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
from ha_stubs import ConfigEntry, FakeHass, check  # noqa: E402

from zota_smart.coordinator import ZotaCoordinator  # noqa: E402
from zota_smart import protocol  # noqa: E402
from zota_smart.state import state_from_info  # noqa: E402

CMD_STAGE = 1
PSK = "00" * 32


class FakeTransport:
    """Транспорт-заглушка: send_frame всегда успешен, кадры запоминаются."""

    is_connected = True

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send_frame(self, link: int, opcode: int, payload: bytes = b"") -> bool:
        self.sent.append(bytes(payload))
        return True


def make_coordinator(power_kw: int = 15) -> tuple[ZotaCoordinator, FakeTransport]:
    """Собрать координатор с фейковым транспортом (без TLS/сети)."""
    hass = FakeHass()
    entry = ConfigEntry(data={"psk": PSK, "power_kw": power_kw})
    transport = FakeTransport()
    return ZotaCoordinator(hass, entry, transport=transport), transport


def ack_payload(cmd_echo: int, result: int = 0) -> bytes:
    """Payload ACK: <u16 cmd_echo><u16 result>."""
    return struct.pack("<HH", cmd_echo, result)


async def case_two_writes_no_loss() -> None:
    """Две быстрые записи с одним cmd: первая не теряется, hang нет."""
    print("--- case 1: сериализация двух записей ---")
    coord, transport = make_coordinator()
    # Вторая команда стартует до ACK первой — раньше это перезаписывало
    # pending future первой, и её await висел бы до таймаута.
    task1 = asyncio.create_task(coord.async_send_write(CMD_STAGE, 2))
    task2 = asyncio.create_task(coord.async_send_write(CMD_STAGE, 3))
    await asyncio.sleep(0.05)  # первая отправлена и ждёт ACK, вторая ждёт лок
    check(len(transport.sent) == 1, "до ACK первой отправлена ровно одна команда")
    coord._handle_ack(ack_payload(CMD_STAGE))  # ACK первой
    result1 = await asyncio.wait_for(task1, 2.0)
    coord._handle_ack(ack_payload(CMD_STAGE))  # ACK второй
    result2 = await asyncio.wait_for(task2, 2.0)
    print(f"  result1 = {result1}, result2 = {result2}, sent = {len(transport.sent)}")
    check(result1 == 0, "первая команда не получила ACK (result=%r)" % result1)
    check(result2 == 0, "вторая команда не получила ACK (result=%r)" % result2)
    check(len(transport.sent) == 2, "отправлены обе команды, а не %d" % len(transport.sent))
    check(not coord._pending, "_pending пуст после обоих ACK: %r" % coord._pending)
    print("  case 1: OK\n")


def case_power_18kw() -> None:
    """power_kw=18 → ступени 6/12/18 кВт; дефолт 15 кВт не меняется."""
    print("--- case 2: power_kw=18 → 6/12/18 ---")
    info = bytearray(53)
    coord, _ = make_coordinator(power_kw=18)
    check(coord.kw_per_stage == 6, "kw_per_stage: ожидалось 6, получено %d" % coord.kw_per_stage)
    for stage in (1, 2, 3):
        info[3] = stage  # активная ступень
        info[4] = stage  # уставка
        state = state_from_info(bytes(info), coord.kw_per_stage)
        check(
            state.power_active_kw == stage * 6,
            "power_active_kw: ступень %d → %d, получено %d"
            % (stage, stage * 6, state.power_active_kw),
        )
        check(
            state.power_kw == stage * 6,
            "power_kw: ступень %d → %d, получено %d" % (stage, stage * 6, state.power_kw),
        )
        check(
            state.stage_name == "%d кВт" % (stage * 6),
            "stage_name: ожидалось %r, получено %r"
            % ("%d кВт" % (stage * 6), state.stage_name),
        )
    coord15, _ = make_coordinator(power_kw=15)
    check(coord15.kw_per_stage == 5, "15 кВт: kw_per_stage должен остаться 5")
    info[3] = info[4] = 1
    state15 = state_from_info(bytes(info), coord15.kw_per_stage)
    check(state15.power_active_kw == 5, "15 кВт: ступень 1 должна давать 5 кВт")
    check(state15.stage_name == "5 кВт", "15 кВт: подпись «5 кВт», получено %r" % state15.stage_name)
    # Координатор передаёт kw_per_stage в разбор info-ACK (эмулируем кадр
    # ступени 3 — «Мощность» 18 кВт).
    info[3] = info[4] = 3
    coord._handle_ack(ack_payload(0) + bytes(info))
    state = coord.data
    check(state is not None and state.power_active_kw == 18, "coordinator.data: 18 кВт на ступени 3")
    print("  case 2: OK\n")


def case_frame_log_option() -> None:
    """Лог кадров пишется только при включённой опции; ротация в .old."""
    print("--- case 3: опция frame_log ---")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "zota_frames.log")
        old_path = path + ".old"
        orig_path, orig_max = protocol.FRAME_LOG_PATH, protocol.FRAME_LOG_MAX_BYTES
        protocol.FRAME_LOG_PATH = path
        protocol.FRAME_LOG_MAX_BYTES = 10  # маленький порог для проверки ротации
        try:
            protocol.set_frame_logging(False)
            protocol.log_frame("M2H", b"\x01\x02")
            check(not os.path.exists(path), "при выключенной опции файл создаваться не должен")
            protocol.set_frame_logging(True)
            protocol.log_frame("M2H", b"\x01\x02")  # строка длиннее 10 байт
            protocol.log_frame("M2H", b"\x03\x04")  # триггер ротации
            check(os.path.exists(old_path), "ротация: .old не создан")
            check(os.path.exists(path), "после ротации лог начат заново")
            check(len(open(path).readlines()) == 1, "в новом логе одна запись")
            with open(old_path, encoding="utf-8") as fh:
                first = fh.readline()
            check("M2H" in first, "в .old попала запись до ротации")
            protocol.set_frame_logging(False)  # не течёт между тестами
        finally:
            protocol.FRAME_LOG_PATH, protocol.FRAME_LOG_MAX_BYTES = orig_path, orig_max
    print("  case 3: OK\n")


async def async_main() -> int:
    print("=== test_runtime_fixes: фиксы f22 ===\n")
    await case_two_writes_no_loss()
    case_power_18kw()
    case_frame_log_option()
    print("=== ALL CASES PASSED ===")
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
