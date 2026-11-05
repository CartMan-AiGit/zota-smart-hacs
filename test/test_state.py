#!/usr/bin/env python3
"""Standalone-тест чистой логики разбора info-блока (state.py).

Запуск (без pytest, без homeassistant):

    python3 test/test_state.py

Проверяются три случая:

1. Базовый блоб: ступень 0, уставка 61, подача 21, обратка 20, насос 1
   включён, нагрев выключен, реле выключены, ошибок нет.
2. Конфликт ``[29]=2`` (насос 1 выкл) при ``bit0`` маски ``[37]``=1 —
   приоритет у ``[29]``; плюс проверка насоса 2 и реле по битам.
3. Ошибка ``[48]=3`` — ``errors_summary`` не «ОК».
4. Нагрев определяется текущей (активной) ступенью ``[3]``, а не битом 7 маски.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "zota_smart")
)

from state import INFO_SIZE, ZotaState, state_from_info  # noqa: E402


def make_blob(**fields: int) -> bytes:
    """Собрать info-блок (53 байта) с заданными полями.

    Незаданные поля — нули. Значения полей, не перечисленные в ТЗ,
    игнорируются.
    """
    blob = bytearray(INFO_SIZE)
    for index, value in fields.items():
        blob[int(index.lstrip("_"))] = value
    return bytes(blob)


def show(title: str, state: ZotaState) -> None:
    """Напечатать фактические значения полей состояния."""
    print(f"--- {title} ---")
    print(f"  stage        = {state.stage}")
    print(f"  active_stage = {state.active_stage}")
    print(f"  setpoint     = {state.setpoint}")
    print(f"  temp_return  = {state.temp_return}")
    print(f"  temp_supply  = {state.temp_supply}")
    print(f"  pump1        = {state.pump1}")
    print(f"  pump2        = {state.pump2}")
    print(f"  heating      = {state.heating}")
    print(f"  relay1       = {state.relay1}")
    print(f"  relay2       = {state.relay2}")
    print(f"  raw37        = {state.raw37}")
    print(f"  pump1_mode   = {state.pump1_mode_byte}")
    print(f"  pump1_relay  = {state.pump1_relay}")
    print(f"  diag         = {state.diag}")
    print(f"  errors       = {state.errors}")
    print(f"  error_offsets = {state.error_offsets}")
    print(f"  errors_summary = {state.errors_summary}")
    print(f"  stage_name   = {state.stage_name}")
    print(f"  power_kw     = {state.power_kw}")
    print(f"  power_active_kw = {state.power_active_kw}")


def check(condition: bool, message: str) -> None:
    """Проверка с падением при ошибке."""
    if not condition:
        raise AssertionError(message)


def case1_baseline() -> None:
    """Базовый блоб: [3]=0 (текущая),[4]=0 (уставка),[5]=61,[15]=20,[21]=21,
    [29]=1,[37]=0x11,[46]=180."""
    blob = make_blob(
        _3=0,
        _4=0,
        _5=61,
        _15=20,
        _21=21,
        _29=1,
        _37=0x11,
        _46=180,
    )
    state = state_from_info(blob)
    show("case 1: baseline", state)

    check(state.stage == 0, f"stage: ожидалось 0, получено {state.stage}")
    check(state.active_stage == 0, f"active_stage: ожидалось 0, получено {state.active_stage}")
    check(state.setpoint == 61, f"setpoint: ожидалось 61, получено {state.setpoint}")
    check(state.temp_supply == 21, f"temp_supply: ожидалось 21, получено {state.temp_supply}")
    check(state.temp_return == 20, f"temp_return: ожидалось 20, получено {state.temp_return}")
    check(state.pump1 is True, f"pump1: ожидалось True, получено {state.pump1}")
    check(state.pump2 is False, f"pump2: ожидалось False, получено {state.pump2}")
    check(state.heating is False, f"heating: ожидалось False, получено {state.heating}")
    check(state.relay1 is False, f"relay1: ожидалось False, получено {state.relay1}")
    check(state.relay2 is False, f"relay2: ожидалось False, получено {state.relay2}")
    check(state.raw37 == 0x11, f"raw37: ожидалось 0x11, получено {state.raw37:#x}")
    check(state.diag == 180, f"diag: ожидалось 180, получено {state.diag}")
    check(state.errors == (), f"errors: ожидалось (), получено {state.errors}")
    check(state.errors_summary == "ОК", f"errors_summary: ожидалось «ОК», получено {state.errors_summary}")
    check(state.has_errors is False, f"has_errors: ожидалось False, получено {state.has_errors}")
    check(state.stage_name == "Выкл", f"stage_name: ожидалось «Выкл», получено {state.stage_name}")
    check(state.power_kw == 0, f"power_kw: ожидалось 0, получено {state.power_kw}")
    print("  case 1: OK\n")


def case2_conflict_pump1() -> None:
    """Конфликт: [29]=2 (насос 1 выкл), но бит 0 маски [37]=1.

    Приоритет у поля [29] — оно прямое значение состояния насоса,
    бит 0 маски — производный признак реле (§11.12.2).

    Раскладка ступеней — по живой валидации 2026-11-05: ``[3]`` — текущая
    (активная), ``[4]`` — установленная (уставка).
    """
    blob = make_blob(
        _3=1,
        _4=1,
        _5=57,
        _15=52,
        _21=60,
        _29=2,
        _37=0x12,
        _46=180,
    )
    state = state_from_info(blob)
    show("case 2: [29]=2 против bit0 маски", state)

    check(state.pump1 is False, f"pump1: ожидалось False (приоритет [29]), получено {state.pump1}")
    check(state.raw37 == 0x12, f"raw37: ожидалось 0x12, получено {state.raw37:#x}")
    check(state.pump2 is True, f"pump2: ожидалось True (bit1), получено {state.pump2}")
    check(state.heating is True, f"heating: ожидалось True (active_stage=1), получено {state.heating}")
    check(state.relay1 is False, f"relay1: ожидалось False (bit5=0), получено {state.relay1}")
    check(state.relay2 is False, f"relay2: ожидалось False (bit6=0), получено {state.relay2}")
    check(state.stage == 1, f"stage: ожидалось 1, получено {state.stage}")
    check(state.active_stage == 1, f"active_stage: ожидалось 1, получено {state.active_stage}")
    check(state.stage_name == "5 кВт", f"stage_name: ожидалось «5 кВт», получено {state.stage_name}")
    check(state.power_kw == 5, f"power_kw: ожидалось 5, получено {state.power_kw}")
    check(state.errors == (), f"errors: ожидалось (), получено {state.errors}")
    print("  case 2: OK\n")


def case3_error() -> None:
    """Ошибка [48]=3 → errors_summary не «ОК»."""
    blob = make_blob(
        _3=0,
        _5=50,
        _29=2,
        _37=0x00,
        _46=180,
        _48=3,
    )
    state = state_from_info(blob)
    show("case 3: ошибка [48]=3", state)

    check(state.errors == (3,), f"errors: ожидалось (3,), получено {state.errors}")
    check(state.has_errors is True, f"has_errors: ожидалось True, получено {state.has_errors}")
    check(state.errors_summary != "ОК", f"errors_summary: не должен быть «ОК», получен {state.errors_summary}")
    check("E48=3" in state.errors_summary, f"errors_summary: ожидалось «E48=3» в {state.errors_summary}")
    print("  case 3: OK\n")


def case4_mask_bits() -> None:
    """Все биты маски [37]: насосы, реле; нагрев — по активной ступени."""
    blob = make_blob(_37=0xE3, _29=1, _46=180)
    state = state_from_info(blob)
    show("case 4: маска 0xE3", state)

    check(state.pump1 is True, f"pump1: ожидалось True, получено {state.pump1}")
    check(state.pump2 is True, f"pump2: ожидалось True (bit1), получено {state.pump2}")
    check(state.relay1 is True, f"relay1: ожидалось True (bit5), получено {state.relay1}")
    check(state.relay2 is True, f"relay2: ожидалось True (bit6), получено {state.relay2}")
    check(state.heating is False, f"heating: ожидалось False (active_stage=0), получено {state.heating}")
    check(state.raw37 == 0xE3, f"raw37: ожидалось 0xE3, получено {state.raw37:#x}")
    check(state.pump1_mode_byte == 1, f"pump1_mode_byte: ожидалось 1, получено {state.pump1_mode_byte}")
    check(state.pump1_relay is True, f"pump1_relay: ожидалось True (bit0), получено {state.pump1_relay}")
    print("  case 4: OK\n")


def case6_heating_by_active_stage() -> None:
    """Нагрев = текущая (активная) ступень ``[3]`` > 0, бит 7 маски не участвует.

    Живая валидация 2026-11-05: котёл греет (реле ТЭНов работают, вода
    греется), а бит 7 маски ``[37]`` = 0 — «Нагрев» показывал «Не работает».
    """
    # Текущая ступень 1, бит 7 маски сброшен → нагрев True.
    blob = make_blob(_3=1, _4=1, _29=1, _37=0x01, _46=180)
    state = state_from_info(blob)
    show("case 6: active_stage=1, бит 7 = 0", state)
    check(state.active_stage == 1, f"active_stage: ожидалось 1, получено {state.active_stage}")
    check(state.raw37 == 0x01, f"raw37: ожидалось 0x01, получено {state.raw37:#x}")
    check(state.heating is True, f"heating: ожидалось True, получено {state.heating}")

    # Текущая ступень 0, бит 7 маски выставлен → нагрев False.
    blob = make_blob(_3=0, _4=0, _29=2, _37=0x81, _46=180)
    state = state_from_info(blob)
    show("case 6b: active_stage=0, бит 7 = 1", state)
    check(state.active_stage == 0, f"active_stage: ожидалось 0, получено {state.active_stage}")
    check(state.raw37 == 0x81, f"raw37: ожидалось 0x81, получено {state.raw37:#x}")
    check(state.heating is False, f"heating: ожидалось False, получено {state.heating}")

    # Ключевая проверка раскладки: уставка 3, а текущая 0 → «Нагрев» False,
    # «Мощность: уставка» 15 кВт, «Мощность: текущая» 0 кВт (живой кейс
    # веб ZOTA 2026-11-05: уставка 15.0 кВт / текущая 0.0 кВт).
    blob = make_blob(_3=0, _4=3, _29=2, _37=0x00, _46=180)
    state = state_from_info(blob)
    show("case 6c: уставка 3 ([4]=3), текущая 0 ([3]=0)", state)
    check(state.stage == 3, f"stage: ожидалось 3, получено {state.stage}")
    check(state.active_stage == 0, f"active_stage: ожидалось 0, получено {state.active_stage}")
    check(state.heating is False, f"heating: ожидалось False, получено {state.heating}")
    check(state.power_kw == 15, f"power_kw: ожидалось 15, получено {state.power_kw}")
    check(
        state.power_active_kw == 0,
        f"power_active_kw: ожидалось 0, получено {state.power_active_kw}",
    )
    check(state.stage_name == "15 кВт", f"stage_name: ожидалось «15 кВт», получено {state.stage_name}")
    print("  case 6: OK\n")


def case7_pump1_sources() -> None:
    """Оба источника по насосу 1 доступны: сырой ``[29]`` и бит 0 ``[37]``."""
    blob = make_blob(_29=2, _37=0x01, _46=180)
    state = state_from_info(blob)
    show("case 7: [29]=2, бит 0 = 1", state)
    check(state.pump1_mode_byte == 2, f"pump1_mode_byte: ожидалось 2, получено {state.pump1_mode_byte}")
    check(state.pump1_relay is True, f"pump1_relay: ожидалось True, получено {state.pump1_relay}")
    check(state.pump1 is False, f"pump1: ожидалось False (приоритет [29]), получено {state.pump1}")

    blob = make_blob(_29=1, _37=0x00, _46=180)
    state = state_from_info(blob)
    show("case 7b: [29]=1, бит 0 = 0", state)
    check(state.pump1_mode_byte == 1, f"pump1_mode_byte: ожидалось 1, получено {state.pump1_mode_byte}")
    check(state.pump1_relay is False, f"pump1_relay: ожидалось False, получено {state.pump1_relay}")
    check(state.pump1 is True, f"pump1: ожидалось True, получено {state.pump1}")
    print("  case 7: OK\n")


def case8_power_split() -> None:
    """Мощность разделена: текущая — из ``[3]``, уставка — из ``[4]``.

    Котёл регулирует нагрев по целевой температуре: уставка (установленная
    ступень) задаёт потолок, а фактически включённых ТЭНов может быть
    меньше — 15 → 10 → 5 → 0.

    Раскладка — по живой валидации 2026-11-05: ``[3]`` — текущая ступень,
    ``[4]`` — уставка.
    """
    blob = make_blob(_3=2, _4=3, _29=1, _37=0x01, _46=180)
    state = state_from_info(blob)
    show("case 8: уставка 3, текущая 2", state)
    check(state.power_kw == 15, f"power_kw: ожидалось 15, получено {state.power_kw}")
    check(
        state.power_active_kw == 10,
        f"power_active_kw: ожидалось 10, получено {state.power_active_kw}",
    )

    for active, expected in ((3, 15), (2, 10), (1, 5), (0, 0)):
        blob = make_blob(_3=active, _4=3, _29=1, _37=0x01, _46=180)
        state = state_from_info(blob)
        check(
            state.power_active_kw == expected,
            f"power_active_kw при active={active}: ожидалось {expected}, "
            f"получено {state.power_active_kw}",
        )
        check(
            state.power_kw == 15,
            f"power_kw при active={active}: ожидалось 15, получено {state.power_kw}",
        )
    print("  case 8: OK\n")


def case5_short_blob() -> None:
    """Короткий блоб → ValueError, а не молчаливый разбор."""
    try:
        state_from_info(b"\x00" * 10)
    except ValueError as err:
        print(f"--- case 5: короткий блоб ---\n  ValueError: {err}\n  case 5: OK\n")
        return
    raise AssertionError("ожидался ValueError на коротком блобе")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_state: разбор info-блока ZOTA Smart ===\n")
    case1_baseline()
    case2_conflict_pump1()
    case3_error()
    case4_mask_bits()
    case5_short_blob()
    case6_heating_by_active_stage()
    case7_pump1_sources()
    case8_power_split()
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
