#!/usr/bin/env python3
"""Извлечь TLS-PSK модуля ZOTA из бинарного контейнера прошивки.

PSK (32 байта) хранится в ресурсной секции контейнера прошивки платы
индикации: ASCII-маркер ``b"Smart key\\x00"``, а сразу за ним — 32 байта
ключа.  Это единственный источник ключа: он не передаётся по сети и не
подбирается.

Утилита работает с любым из файлов:

* уже расшифрованная ресурсная секция / контейнер (``container_dec.bin``);
* сырой контейнер прошивки (``.zap``) — тогда ключ ищется в
  XOR-зашифрованном виде и выводится как есть; чтобы получить ресурсную
  секцию, сначала расшифруйте контейнер (см. README, раздел «Получение PSK»).

Файл не содержит ни одного ключа и ни одного секрета: ключ только читается
из переданного пользователем файла.

Использование::

    python3 tools/extract_psk.py ПУТЬ_К_ФАЙЛУ
"""

import sys

MARKER = b"Smart key\x00"
PSK_LEN = 32


def find_psk(data):
    """Вернуть список (смещение, 32 байта) для всех вхождений маркера."""
    hits = []
    start = 0
    while True:
        off = data.find(MARKER, start)
        if off < 0:
            break
        key_at = off + len(MARKER)
        hits.append((off, data[key_at:key_at + PSK_LEN]))
        start = off + 1
    return hits


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(
            "usage: python3 tools/extract_psk.py FILE\n"
            "  FILE — бинарный контейнер/ресурсная секция прошивки\n"
        )
        return 2

    path = argv[1]
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        sys.stderr.write("error: cannot read %s: %s\n" % (path, exc))
        return 1

    hits = find_psk(data)

    if not hits:
        sys.stderr.write(
            "error: marker b'Smart key\\x00' not found in %s (%d bytes)\n"
            % (path, len(data))
        )
        sys.stderr.write(
            "hints:\n"
            "  - the file may still be encrypted: the firmware container is\n"
            "    XOR-obfuscated (44-byte record grid, 22-byte key). Decrypt the\n"
            "    container first and point this script at the decrypted section.\n"
            "  - look for the resource container inside the firmware package\n"
            "    (for PO Smart v2 it is 'smart.zap') and extract its resource\n"
            "    section.\n"
            "  - the marker is ASCII 'Smart key' followed by a NUL byte, and the\n"
            "    32 key bytes follow it immediately.\n"
        )
        return 1

    print("file: %s (%d bytes)" % (path, len(data)))
    print("matches: %d" % len(hits))
    for off, key in hits:
        if len(key) < PSK_LEN:
            sys.stderr.write(
                "warning: marker at 0x%x is truncated, only %d of %d key bytes\n"
                % (off, len(key), PSK_LEN)
            )
            continue
        print("offset: 0x%x" % off)
        print("psk: %s" % key.hex())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
