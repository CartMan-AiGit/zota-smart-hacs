#!/usr/bin/env python3
"""Standalone-раннер TLS-PSK сервера ZOTA Smart — для проверки в HA-контейнере.

Запуск (из одной папки, где лежат tls_server.py и protocol.py):

    python3 zota_tls_test.py

Никаких импортов homeassistant: поднимается голый TLSServerTransport на
127.0.0.1:11978, печатаются события (identity, connect, disconnect, каждый
кадр). На read-кадр link=1 opcode=0 отвечает ACK.

Файл копируется в контейнер вместе с tls_server.py и protocol.py — поэтому
импорт protocol сделан «как в пакете, так и без него».
"""

from __future__ import annotations

import os
import signal
import struct
import sys
import threading

# --- Импорт protocol.py и tls_server.py без пакета и внутри пакета ---------

try:  # внутри пакета интеграции
    from .protocol import build_ack
    from .tls_server import TLSServerTransport
except ImportError:  # standalone-режим: файлы лежат рядом
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from protocol import build_ack  # type: ignore[no-redef]
    from tls_server import TLSServerTransport  # type: ignore[no-redef]


HOST = "127.0.0.1"
PORT = 11978

#: Плейсхолдер-PSK (64 hex-символа). Настоящий ключ своего модуля вводится
#: в настройках интеграции; в репозитории его быть не должно.
PSK_HEX = "11" * 32
PSK_IDENTITY = "Smart key"

#: link=1 — плоский командный кадр; opcode=0 — команда/запись.
LINK_FLAT = 1
OPCODE_COMMAND = 0

_stop = threading.Event()


def log(msg: str) -> None:
    """Печать с префиксом."""
    print("[zota-tls-test] %s" % msg, flush=True)


def on_client() -> None:
    """Клиент прошёл рукопожатие."""
    log("CONNECT: клиент установил TLS-соединение")


def on_disconnect() -> None:
    """Соединение разорвано."""
    log("DISCONNECT: соединение закрыто")


def on_frame(link: int, opcode: int, payload: bytes) -> None:
    """Разобранный кадр от клиента.

    На read-кадр link=1 opcode=0 отвечаем ACK: build_ack(cmd, 0),
    где cmd — первое u16 payload'а.
    """
    log("FRAME: link=%d opcode=%d len=%d payload=%s" % (link, opcode, len(payload), payload.hex()))

    if link != LINK_FLAT or opcode != OPCODE_COMMAND:
        log("  → не read-кадр, ответа нет")
        return
    if len(payload) < 2:
        log("  → payload короче u16 cmd, ответа нет")
        return

    (cmd,) = struct.unpack_from("<H", payload, 0)
    log("  → read cmd=%d, отвечаю ACK(result=0)" % cmd)
    server.send_frame(LINK_FLAT, 1, build_ack(cmd, 0)[6:])


def _handle_signal(signum, _frame) -> None:
    """SIGINT/SIGTERM → аккуратная остановка."""
    log("Получен сигнал %d, останавливаюсь" % signum)
    _stop.set()


def main() -> int:
    """Поднять сервер и крутиться до SIGINT."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    global server
    server = TLSServerTransport(
        host=HOST,
        port=PORT,
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        on_client=on_client,
        on_frame=on_frame,
        on_disconnect=on_disconnect,
    )
    server.start()
    log("Сервер на %s:%d, identity=%r, psk=%s…%s" % (HOST, PORT, PSK_IDENTITY, PSK_HEX[:8], PSK_HEX[-4:]))
    log("Жду подключения (openssl s_client -connect %s:%d -psk ... -psk_identity 'Smart key' "
        "-cipher '@SECLEVEL=0:PSK-AES128-CBC-SHA256')" % (HOST, PORT))

    try:
        while not _stop.is_set():
            _stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log("Останавливаю сервер…")
        server.stop()
        log("Остановлено")
    return 0


server = None  # type: ignore[assignment]

if __name__ == "__main__":
    sys.exit(main())
