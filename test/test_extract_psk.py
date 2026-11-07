#!/usr/bin/env python3
"""Standalone-тест утилиты tools/extract_psk.py.

Запуск (без pytest):

    python3 test/test_extract_psk.py

Проверяются три случая:

1. Одно вхождение маркера — печатаются смещение и 32 байта ключа в hex.
2. Несколько вхождений — печатаются все.
3. Маркер не найден — понятная ошибка с подсказками и ненулевой код выхода.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

TOOL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "extract_psk.py"
)
MARKER = b"Smart key\x00"
STUB = bytes(range(32))


def run_tool(payload: bytes) -> tuple[int, str, str]:
    """Запустить утилиту на временном файле с заданным содержимым."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
        fh.write(payload)
        path = fh.name
    try:
        proc = subprocess.run(
            [sys.executable, TOOL, path],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.unlink(path)
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    # --- case 1: одно вхождение -------------------------------------------
    payload = b"\x00" * 16 + MARKER + STUB + b"\xff" * 8
    rc, out, err = run_tool(payload)
    assert rc == 0, "case 1: exit=%d, stderr=%s" % (rc, err)
    assert "matches: 1" in out, "case 1: нет 'matches: 1' в выводе:\n%s" % out
    assert "offset: 0x10" in out, "case 1: нет смещения 0x10:\n%s" % out
    assert STUB.hex() in out, "case 1: нет hex ключа:\n%s" % out
    print("  case 1: OK — одно вхождение, смещение 0x10, 32 байта в hex")

    # --- case 2: несколько вхождений --------------------------------------
    payload = MARKER + b"\x00" * 32 + b"xx" + MARKER + STUB
    rc, out, err = run_tool(payload)
    assert rc == 0, "case 2: exit=%d, stderr=%s" % (rc, err)
    assert "matches: 2" in out, "case 2: нет 'matches: 2' в выводе:\n%s" % out
    assert out.count("psk: ") == 2, "case 2: напечатано не 2 ключа:\n%s" % out
    assert STUB.hex() in out, "case 2: нет hex второго ключа:\n%s" % out
    print("  case 2: OK — два вхождения, напечатаны оба ключа")

    # --- case 3: маркер не найден ------------------------------------------
    rc, out, err = run_tool(b"no marker here at all")
    assert rc == 1, "case 3: ожидался exit=1, получено %d" % rc
    assert "not found" in err, "case 3: нет сообщения об отсутствии маркера:\n%s" % err
    assert "hints:" in err, "case 3: нет подсказок:\n%s" % err
    assert "encrypt" in err.lower(), "case 3: нет подсказки про шифрование:\n%s" % err
    print("  case 3: OK — маркер не найден: ошибка + подсказки, exit=1")

    print("\n--- ALL CASES PASSED ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
