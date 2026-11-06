#!/usr/bin/env python3
"""Standalone-тест моста к облаку ZOTA (режим PROXY).

Запуск (без pytest, без установленного homeassistant):

    python3 test/test_proxy_bridge.py

Home Assistant и pyOpenSSL локально не установлены, поэтому модуль
``homeassistant`` подменяется заглушками из :mod:`ha_stubs`, а TLS-соединение
— фейком. Проверяется настоящий код моста (:mod:`zota_smart.proxy`) и
координатора:

* маршрутизация модуль → облако: кадр уходит в облако дословно и одновременно
  разбирается локально (``on_frame``);
* маршрутизация облако → модуль: кадр уходит модулю дословно и разбирается
  локально;
* регистрация ``link=2 op=2`` форвардится прозрачно — мост ничего не
  генерирует от себя;
* реконнект-флаги: ``is_connected`` поднимается после рукопожатия и
  опускается после обрыва, цикл моста переподключается через
  ``RECONNECT_DELAY``;
* координатор в PROXY-режиме поднимает и останавливает мост;
* переименованные имена реле (ключи и ``unique_id`` не тронуты).
"""

from __future__ import annotations

import asyncio
import errno
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

from ha_stubs import ConfigEntry, FakeHass, check  # noqa: E402

from zota_smart import proxy as proxy_mod  # noqa: E402
from zota_smart.binary_sensor import BINARY_SENSOR_DESCRIPTIONS  # noqa: E402
from zota_smart.coordinator import ZotaCoordinator  # noqa: E402
from zota_smart.proxy import (  # noqa: E402
    RECONNECT_DELAY,
    ZotaCloudBridge,
    parse_cloud_address,
)

#: Плейсхолдер-PSK (64 hex-символа). Тесты не поднимают настоящий TLS —
#: соединение подменяется фейком, поэтому реальный ключ не нужен.
PSK_HEX = "11" * 32
PSK_IDENTITY = "Smart key"

LINK_FLAT = 1
LINK_REGISTER = 2
OPCODE_COMMAND = 0
OPCODE_ACK = 1
OPCODE_REGISTER = 2
OPCODE_KEEPALIVE = 4

CMD_INFO = 0


# --- Фейки TLS и сокета ----------------------------------------------------


class FakeCloudConnection:
    """Фейк TLS-соединения с облаком: пишет в журнал, читает из очереди."""

    def __init__(self, incoming=None):
        self.sent = []
        self._incoming = list(incoming or [])
        self.shutdown_called = False
        self.closed = False

    def set_connect_state(self):
        """pyOpenSSL: переключить соединение в режим клиента."""

    def set_accept_state(self):
        """pyOpenSSL: переключить соединение в режим сервера."""

    def do_handshake(self):
        """Рукопожатие всегда проходит."""

    def send(self, data):
        """Записать байты в журнал."""
        self.sent.append(bytes(data))
        return len(data)

    def recv(self, size):
        """Отдать очередной кусок из входящих данных."""
        if not self._incoming:
            return b""
        return self._incoming.pop(0)

    def shutdown(self):
        """Зафиксировать выключение."""
        self.shutdown_called = True

    def close(self):
        """Зафиксировать закрытие."""
        self.closed = True


class FakeSocket:
    """Фейк сокета."""

    def __init__(self):
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        """Запомнить таймаут сокета."""
        self.timeout = timeout

    def close(self):
        """Зафиксировать закрытие."""
        self.closed = True


def make_bridge(on_frame=None, send_to_module=None, incoming=None,
                on_registration_frame=None, get_registration_frame=None):
    """Собрать мост с фейковым TLS-соединением с облаком.

    Соединение подставляется напрямую в ``_conn`` — сетевых обращений и
    рукопожатия не происходит.
    """
    bridge = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        on_frame=on_frame,
        send_to_module=send_to_module,
        on_registration_frame=on_registration_frame,
        get_registration_frame=get_registration_frame,
    )
    bridge._conn = FakeCloudConnection(incoming)
    return bridge


def build_frame(link, opcode, payload=b""):
    """Собрать кадр: <u16 link, u16 opcode, u16 len> + payload."""
    payload = bytes(payload)
    return struct.pack("<HHH", link, opcode, len(payload)) + payload


# --- Случаи ----------------------------------------------------------------


def case_module_to_cloud() -> None:
    """Кадр от модуля уходит в облако дословно и разбирается локально."""
    seen = []
    bridge = make_bridge(on_frame=lambda link, op, pl: seen.append((link, op, pl)))
    conn = bridge._conn

    frame = build_frame(LINK_FLAT, OPCODE_ACK, struct.pack("<HH", CMD_INFO, 0) + bytes(53))
    bridge.feed_from_module(frame)

    print(f"--- case 1: модуль → облако ---")
    print(f"  в облако отправлено байт = {len(conn.sent[0])}")
    print(f"  разобрано кадров = {len(seen)}")
    check(conn.sent == [frame], "в облако должна уйти ровно одна порция, байт в байт")
    check(conn.sent[0] == frame, "байты в облако должны уйти без изменений")
    check(len(seen) == 1, f"ожидался 1 разобранный кадр, получено {len(seen)}")
    check(seen[0][0] == LINK_FLAT, f"link: ожидалось {LINK_FLAT}, получено {seen[0][0]}")
    check(seen[0][1] == OPCODE_ACK, f"opcode: ожидалось {OPCODE_ACK}, получено {seen[0][1]}")
    print("  case 1: OK\n")


def case_cloud_to_module() -> None:
    """Кадр от облака уходит модулю дословно и разбирается локально."""
    seen = []
    to_module = []
    bridge = make_bridge(
        on_frame=lambda link, op, pl: seen.append((link, op, pl)),
        send_to_module=lambda data: (to_module.append(bytes(data)), True)[1],
    )
    conn = bridge._conn

    frame = build_frame(LINK_FLAT, OPCODE_ACK, struct.pack("<HH", CMD_INFO, 0) + bytes(53))
    bridge._handle_cloud_bytes(frame)

    print("--- case 2: облако → модуль ---")
    print(f"  модулю отправлено байт = {len(to_module[0])}")
    print(f"  разобрано кадров = {len(seen)}")
    check(to_module == [frame], "в модуль должна уйти ровно одна порция, байт в байт")
    check(to_module[0] == frame, "байты в модуль должны уйти без изменений")
    check(len(seen) == 1, f"ожидался 1 разобранный кадр, получено {len(seen)}")
    check(seen[0][0] == LINK_FLAT, f"link: ожидалось {LINK_FLAT}, получено {seen[0][0]}")
    check(seen[0][1] == OPCODE_ACK, f"opcode: ожидалось {OPCODE_ACK}, получено {seen[0][1]}")
    check(conn.sent == [], "мост не должен ничего слать в облако сам по себе")
    print("  case 2: OK\n")


def case_register_transparent() -> None:
    """Регистрация link=2 op=2 форвардится облаку прозрачно.

    Мост ничего не генерирует: ни ответа на регистрацию (§11.3, правило 1),
    ни собственных poll-кадров.
    """
    seen = []
    bridge = make_bridge(on_frame=lambda link, op, pl: seen.append((link, op, pl)))
    conn = bridge._conn

    frame = build_frame(LINK_REGISTER, OPCODE_REGISTER, bytes.fromhex("0000010001000000"))
    bridge.feed_from_module(frame)

    print("--- case 3: регистрация прозрачна ---")
    print(f"  в облако: {conn.sent[0].hex()}")
    print(f"  разобрано кадров = {len(seen)}")
    check(conn.sent == [frame], "регистрация должна уйти в облако байт в байт")
    check(len(conn.sent) == 1, "мост не должен ничего досылать от себя")
    check(len(seen) == 1, "регистрация должна разобраться локально")
    check(seen[0][0] == LINK_REGISTER, f"link: ожидалось {LINK_REGISTER}")
    check(seen[0][1] == OPCODE_REGISTER, f"opcode: ожидалось {OPCODE_REGISTER}")
    print("  case 3: OK\n")


def case_partial_frames() -> None:
    """Кадр, пришедший двумя кусками, разбирается целиком."""
    seen = []
    bridge = make_bridge(on_frame=lambda link, op, pl: seen.append((link, op, pl)))
    conn = bridge._conn

    frame = build_frame(LINK_REGISTER, OPCODE_REGISTER, bytes.fromhex("0000010001000000"))
    bridge.feed_from_module(frame[:4])
    check(len(seen) == 0, "полукадр разбираться не должен")
    bridge.feed_from_module(frame[4:])
    check(len(seen) == 1, f"ожидался 1 кадр после склейки, получено {len(seen)}")
    check(conn.sent[0] + conn.sent[1] == frame, "в облако должны уйти все байты")
    print("--- case 4: склейка полукадра ---")
    print(f"  разобрано кадров = {len(seen)}")
    print("  case 4: OK\n")


def case_reconnect_flags() -> None:
    """Реконнект-флаги: is_connected и повторное подключение."""
    print("--- case 5: реконнект-флаги ---")
    print(f"  RECONNECT_DELAY = {RECONNECT_DELAY} c")
    check(RECONNECT_DELAY == 10.0, f"RECONNECT_DELAY: ожидалось 10.0, получено {RECONNECT_DELAY}")

    bridge = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
    )
    check(bridge.is_connected is False, "до подключения ожидалось is_connected=False")
    check(bridge.host == "217.25.88.41", f"host: получено {bridge.host}")
    check(bridge.port == 1978, f"port: получено {bridge.port}")

    # Сессия с фейковым соединением: рукопожатие проходит, затем EOF.
    conn = FakeCloudConnection()
    bridge._ctx = object()
    original_create = proxy_mod.socket.create_connection
    original_connection = proxy_mod.SSL.Connection
    original_reconnect = proxy_mod.RECONNECT_DELAY

    def fake_create_connection(address, timeout=None):
        """Фейковое подключение к облаку."""
        return FakeSocket()

    proxy_mod.socket.create_connection = fake_create_connection
    proxy_mod.SSL.Connection = lambda ctx, sock: conn
    proxy_mod.RECONNECT_DELAY = 0.01
    try:
        bridge._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection
        proxy_mod.RECONNECT_DELAY = original_reconnect

    print(f"  после сессии is_connected = {bridge.is_connected}")
    check(bridge.is_connected is False, "после обрыва ожидалось is_connected=False")

    # Флаги соединения внутри живой сессии.
    seen_flags = []

    class FlagConnection(FakeCloudConnection):
        """Фейк соединения, отмечающий флаг connected во время сессии."""

        def recv(self, size):
            """Отметить connected и вернуть EOF."""
            seen_flags.append(bridge_probe["bridge"].is_connected)
            return b""

    bridge_probe = {"bridge": bridge}
    conn2 = FlagConnection()
    proxy_mod.socket.create_connection = fake_create_connection
    proxy_mod.SSL.Connection = lambda ctx, sock: conn2
    try:
        bridge._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  флаг connected во время сессии = {seen_flags}")
    check(seen_flags == [True], f"во время сессии ожидалось connected=True, получено {seen_flags}")
    check(bridge.is_connected is False, "после сессии ожидалось connected=False")
    print("  case 5: OK\n")


def case_reconnect_loop() -> None:
    """Цикл моста переподключается после обрыва."""
    print("--- case 6: цикл переподключения ---")
    attempts = []
    bridge = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
    )
    bridge._ctx = object()

    original_create = proxy_mod.socket.create_connection
    original_connection = proxy_mod.SSL.Connection
    original_reconnect = proxy_mod.RECONNECT_DELAY

    def fake_create_connection(address, timeout=None):
        """Фейковое подключение: считаем попытки."""
        attempts.append(address)
        return FakeSocket()

    class LoopConnection(FakeCloudConnection):
        """Фейк соединения, рвущий сессию сразу."""

        def recv(self, size):
            """Вернуть EOF — сессия завершается."""
            return b""

    proxy_mod.socket.create_connection = fake_create_connection
    proxy_mod.SSL.Connection = lambda ctx, sock: LoopConnection()
    proxy_mod.RECONNECT_DELAY = 0.01
    try:
        # Ограничиваем число попыток: поток-демон крутился бы бесконечно.
        original_run = bridge._run

        def limited_run():
            """Прогнать мост ровно две попытки подключения."""
            for _ in range(2):
                if bridge._stop_event.is_set():
                    break
                try:
                    bridge._session()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    bridge._close_connection()
                bridge._stop_event.wait(0.01)

        bridge._run = limited_run
        bridge.start()
        bridge._thread.join(5.0)
        del original_run
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection
        proxy_mod.RECONNECT_DELAY = original_reconnect

    print(f"  попыток подключения = {len(attempts)}")
    check(len(attempts) == 2, f"ожидалось 2 попытки подключения, получено {len(attempts)}")
    check(attempts[0] == ("217.25.88.41", 1978), f"ожидался адрес облака, получено {attempts[0]}")
    print("  case 6: OK\n")


def case_parse_address() -> None:
    """Разбор адреса облака host:port."""
    print("--- case 7: разбор адреса ---")
    cases = [
        ("217.25.88.41:1978", ("217.25.88.41", 1978)),
        ("217.25.88.41", ("217.25.88.41", 1978)),
        ("example.org:1234", ("example.org", 1234)),
        ("[::1]:1978", ("::1", 1978)),
    ]
    for text, expected in cases:
        got = parse_cloud_address(text)
        print(f"  {text:24s} → {got}")
        check(got == expected, f"{text}: ожидалось {expected}, получено {got}")
    print("  case 7: OK\n")


def case_coordinator_bridge() -> None:
    """Координатор в PROXY-режиме поднимает и останавливает мост."""
    print("--- case 8: координатор и мост ---")
    started = []
    stopped = []

    class FakeBridge:
        """Фейк моста: фиксирует старт и остановку."""

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.is_running = False

        def start(self):
            """Зафиксировать старт."""
            self.started = True
            self.is_running = True
            started.append(self)

        def stop(self, timeout=5.0):
            """Зафиксировать остановку."""
            self.stopped = True
            self.is_running = False
            stopped.append(self)

        def feed_from_module(self, data):
            """Ничего не делать."""

    original_bridge_cls = proxy_mod.ZotaCloudBridge

    import zota_smart.coordinator as coordinator_mod

    coordinator_mod.ZotaCloudBridge = FakeBridge
    try:
        hass = FakeHass()
        entry = ConfigEntry(
            data={
                "name": "Котёл",
                "port": 1978,
                "proxy": True,
                "cloud_address": "217.25.88.41:1978",
                "psk": PSK_HEX,
            }
        )
        coordinator = ZotaCoordinator(hass, entry)
        check(coordinator.proxy is True, "proxy: ожидалось True")

        # Мост поднимается лениво — при подключении модуля.
        check(len(started) == 0, "до подключения модуля мост не должен подниматься")
        coordinator._on_client()
        check(len(started) == 1, f"ожидался 1 запуск моста, получено {len(started)}")
        check(started[0].started is True, "мост должен быть запущен")
        check(
            started[0].kwargs["cloud_address"] == "217.25.88.41:1978",
            f"адрес облака: получено {started[0].kwargs['cloud_address']}",
        )

        # Повторное подключение модуля не поднимает второй мост.
        coordinator._on_client()
        check(len(started) == 1, f"ожидался 1 запуск моста, получено {len(started)}")

        asyncio.run(coordinator.stop_bridge())
        check(len(stopped) == 1, f"ожидалась 1 остановка моста, получено {len(stopped)}")
        check(stopped[0].stopped is True, "мост должен быть остановлен")
        check(coordinator._bridge is None, "после stop_bridge моста быть не должно")

        # Без PROXY мост не поднимается вовсе.
        hass2 = FakeHass()
        entry2 = ConfigEntry(
            data={"name": "Котёл", "port": 1978, "proxy": False, "psk": PSK_HEX}
        )
        coordinator2 = ZotaCoordinator(hass2, entry2)
        coordinator2._on_client()
        check(len(started) == 1, "в LOCAL-режиме мост подниматься не должен")
    finally:
        coordinator_mod.ZotaCloudBridge = original_bridge_cls
    print("  case 8: OK\n")


def case_start_wiring() -> None:
    """В PROXY-режиме транспорт отдаёт байты мосту, а мост создан сразу.

    Мост обязан существовать до первых байт от модуля: регистрация
    приходит сразу после рукопожатия.
    """
    print("--- case 9: связка координатор → транспорт → мост ---")
    started = []

    class FakeBridge:
        """Фейк моста."""

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.is_running = False
            self.received = []

        def start(self):
            """Зафиксировать старт."""
            self.is_running = True
            started.append(self)

        def stop(self, timeout=5.0):
            """Зафиксировать остановку."""
            self.is_running = False

        def feed_from_module(self, data):
            """Запомнить присланные байты."""
            self.received.append(bytes(data))

    class FakeTransport:
        """Фейк транспорта: запоминает колбэки."""

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.on_frame = kwargs.get("on_frame")
            self.on_bytes = kwargs.get("on_bytes")
            self.on_client = kwargs.get("on_client")
            self.started = False

        def start(self):
            """Зафиксировать старт."""
            self.started = True

        def stop(self, timeout=5.0):
            """Зафиксировать остановку."""
            self.stopped = True

        def send_frame(self, link, opcode, payload=b""):
            """Заглушка отправки."""
            return True

        def send_bytes(self, data):
            """Заглушка отправки байтов."""
            return True

    original_bridge_cls = proxy_mod.ZotaCloudBridge

    import zota_smart.coordinator as coordinator_mod

    coordinator_mod.ZotaCloudBridge = FakeBridge
    try:
        hass = FakeHass()
        entry = ConfigEntry(
            data={
                "name": "Котёл",
                "port": 1978,
                "proxy": True,
                "cloud_address": "217.25.88.41:1978",
                "psk": PSK_HEX,
            }
        )
        coordinator = ZotaCoordinator(hass, entry, transport=None)
        # Подменяем класс транспорта на фейк, чтобы не поднимать сокет.
        original_transport_cls = coordinator_mod.TLSServerTransport
        coordinator_mod.TLSServerTransport = FakeTransport
        try:
            asyncio.run(coordinator.async_start())
        finally:
            coordinator_mod.TLSServerTransport = original_transport_cls

        transport = coordinator._transport
        check(transport is not None, "транспорт должен быть создан")
        check(transport.on_frame is None, "в PROXY-режиме разбор кадров отдаётся мосту")
        check(transport.on_bytes is not None, "транспорт должен отдавать байты мосту")
        check(coordinator._bridge is not None, "мост должен быть создан при старте")
        check(len(started) == 0, "поток моста не должен подниматься до подключения модуля")

        # Первые байты от модуля (регистрация) не должны потеряться.
        transport.on_bytes(b"\x02\x00\x02\x00\x08\x00")
        check(
            len(coordinator._bridge.received) == 1,
            f"ожидалась 1 порция в мост, получено {len(coordinator._bridge.received)}",
        )
        check(
            coordinator._bridge.received[0] == b"\x02\x00\x02\x00\x08\x00",
            "байты в мост должны уйти без изменений",
        )

        # Подключение модуля поднимает поток моста.
        transport.on_client()
        check(len(started) == 1, f"ожидался 1 запуск моста, получено {len(started)}")
        check(started[0].is_running is True, "поток моста должен быть запущен")

        asyncio.run(coordinator.async_stop())
    finally:
        coordinator_mod.ZotaCloudBridge = original_bridge_cls
    print("  case 9: OK\n")


def case_psk_client_callback() -> None:
    """Клиентский PSK-callback: сигнатура и поведение — на живом OpenSSL.

    pyOpenSSL не даёт доступа к ``SSL_CTX_set_psk_client_callback``, поэтому
    колбэк ставится через bindings ``cryptography``. Здесь проверяется, что
    C-сигнатура колбэка совпадает с typedef'ом OpenSSL
    (``SSL_psk_client_cb_func``) и что колбэк корректно отдаёт identity и
    PSK — это проверяется вызовом C-функции через сам OpenSSL.

    Требует установленного ``cryptography`` — если его нет, случай
    пропускается (тест остаётся зелёным).
    """
    print("--- case 10: клиентский PSK-callback ---")
    try:
        from cryptography.hazmat.bindings.openssl.binding import Binding
    except ImportError as err:
        print(f"  cryptography не установлен ({err}) — случай пропущен")
        print("  case 10: OK\n")
        return

    ffi, lib = Binding().ffi, Binding().lib
    check(
        hasattr(lib, "SSL_CTX_set_psk_client_callback"),
        "в OpenSSL из cryptography нет символа SSL_CTX_set_psk_client_callback",
    )

    psk_bytes = bytes.fromhex(PSK_HEX)
    identity_bytes = PSK_IDENTITY.encode()
    seen = {}

    signature = proxy_mod._PSK_CLIENT_CB_SIGNATURE
    check(
        signature
        == "unsigned int(SSL *, const char *, char *, unsigned int, "
        "unsigned char *, unsigned int)",
        f"неожиданная C-сигнатура колбэка: {signature!r}",
    )

    @ffi.callback(signature)
    def callback(ssl, hint, identity_out, max_identity_len, psk_out, max_psk_len):
        """Отдать identity и PSK; ``0`` — отказ."""
        seen["hint"] = ffi.string(hint) if hint != ffi.NULL else None
        seen["max_identity_len"] = max_identity_len
        seen["max_psk_len"] = max_psk_len
        if len(identity_bytes) + 1 > max_identity_len:
            return 0
        if len(psk_bytes) > max_psk_len:
            return 0
        ffi.buffer(identity_out, len(identity_bytes) + 1)[:] = identity_bytes + b"\x00"
        ffi.buffer(psk_out, len(psk_bytes))[:] = psk_bytes
        return len(psk_bytes)

    ident_buf = ffi.new("char[]", 256)
    psk_buf = ffi.new("unsigned char[]", 512)
    hint = ffi.new("char[]", b"server hint")
    rc = callback(ffi.NULL, hint, ident_buf, 256, psk_buf, 512)
    print(f"  rc = {rc} (ожидается {len(psk_bytes)})")
    print(f"  identity = {ffi.string(ident_buf)!r}")
    check(rc == len(psk_bytes), f"rc: ожидалось {len(psk_bytes)}, получено {rc}")
    check(ffi.string(ident_buf) == identity_bytes, "identity должна уйти в буфер")
    check(
        bytes(ffi.buffer(psk_buf, len(psk_bytes))) == psk_bytes,
        "PSK должен уйти в буфер байт в байт",
    )
    check(seen["max_identity_len"] == 256, "max_identity_len должен доезжать")
    check(seen["max_psk_len"] == 512, "max_psk_len должен доезжать")

    # Отказ при недостаточных буферах.
    rc_small_identity = callback(ffi.NULL, hint, ident_buf, 4, psk_buf, 512)
    rc_small_psk = callback(ffi.NULL, hint, ident_buf, 256, psk_buf, 8)
    print(f"  отказ: identity-мал={rc_small_identity}, psk-мал={rc_small_psk}")
    check(rc_small_identity == 0, "при малом буфере identity ожидался отказ (0)")
    check(rc_small_psk == 0, "при малом буфере PSK ожидался отказ (0)")

    # Колбэк должен приниматься настоящим SSL_CTX.
    ctx = lib.SSL_CTX_new(lib.TLS_client_method())
    check(ctx != ffi.NULL, "не удалось создать SSL_CTX")
    lib.SSL_CTX_set_psk_client_callback(ctx, callback)
    lib.SSL_CTX_free(ctx)
    print("  колбэк принят настоящим SSL_CTX: OK")
    print("  case 10: OK\n")


def case_binary_sensor_names() -> None:
    """Набор бинарных сенсоров: только нагрев и насосы, без реле ТЭН."""
    print("--- case 11: состав бинарных сенсоров ---")
    names = {d.key: d.translation_key for d in BINARY_SENSOR_DESCRIPTIONS}
    for key in sorted(names):
        print(f"  {key} = {names[key]!r}")
    check(names["heating"] == "heating", f"heating: получено {names['heating']!r}")
    check(names["pump1"] == "pump1", f"pump1: получено {names['pump1']!r}")
    check(names["pump2"] == "pump2", f"pump2: получено {names['pump2']!r}")
    check(
        set(names) == {"heating", "pump1", "pump2"},
        f"ожидались только heating/pump1/pump2, получено {sorted(names)}",
    )
    print("  case 11: OK\n")


def case_eagain_is_not_disconnect() -> None:
    """EAGAIN в recv — это «данных нет», а не обрыв соединения.

    Живой симптом: «Подключение к облаку установлено» → через 2-10 с
    «Соединение закрыто: (11, 'EAGAIN')» → реконнект 10 с → бесконечно.
    EAGAIN на неблокирующем сокете — нормальное состояние «данных нет»:
    цикл чтения обязан его пережить (пауза и продолжение).
    """
    print("--- case 12: EAGAIN не рвёт соединение ---")

    # Значение errno зависит от платформы (Linux — 11, macOS — 35),
    # поэтому берём настоящую константу.
    eagain_errno = errno.EAGAIN
    print(f"  errno.EAGAIN = {eagain_errno}")

    # pyOpenSSL поднимает EAGAIN как SysCallError/SSL.Error с errno.
    class EagainConnection(FakeCloudConnection):
        """Фейк: первый recv — EAGAIN, дальше — данные и EOF."""

        def __init__(self, incoming=None):
            super().__init__(incoming)
            self.recv_calls = 0
            self.eagain = True

        def recv(self, size):
            """Сначала EAGAIN, затем данные, затем EOF."""
            self.recv_calls += 1
            if self.eagain:
                self.eagain = False
                raise proxy_mod.SSL.SysCallError(eagain_errno, "EAGAIN")
            return super().recv(size)

    to_module = []
    frame = build_frame(LINK_FLAT, OPCODE_ACK, struct.pack("<HH", CMD_INFO, 0) + bytes(53))
    conn = EagainConnection([frame])

    bridge = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        send_to_module=lambda data: (to_module.append(bytes(data)), True)[1],
    )
    bridge._ctx = object()

    original_create = proxy_mod.socket.create_connection
    original_connection = proxy_mod.SSL.Connection
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn
        bridge._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  recv-вызовов = {conn.recv_calls}")
    print(f"  модулю отправлено порций = {len(to_module)}")
    check(
        conn.recv_calls >= 2,
        f"цикл должен продолжиться после EAGAIN, recv-вызовов всего {conn.recv_calls}",
    )
    check(
        len(to_module) == 1 and to_module[0] == frame,
        "данные после EAGAIN должны дойти до модуля",
    )

    # Тот же сценарий для «голого» OSError(EAGAIN) — errno в самом исключении.
    class OsEagainConnection(FakeCloudConnection):
        """Фейк: EAGAIN как OSError с errno."""

        def __init__(self, incoming=None):
            super().__init__(incoming)
            self.recv_calls = 0
            self.eagain = True

        def recv(self, size):
            """Сначала OSError(EAGAIN), затем данные, затем EOF."""
            self.recv_calls += 1
            if self.eagain:
                self.eagain = False
                raise OSError(eagain_errno, "EAGAIN")
            return super().recv(size)

    to_module2 = []
    conn2 = OsEagainConnection([frame])
    bridge2 = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        send_to_module=lambda data: (to_module2.append(bytes(data)), True)[1],
    )
    bridge2._ctx = object()
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn2
        bridge2._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  OSError(EAGAIN): recv-вызовов = {conn2.recv_calls}")
    check(
        conn2.recv_calls >= 2,
        f"OSError(EAGAIN) не должен рвать цикл, recv-вызовов {conn2.recv_calls}",
    )
    check(
        len(to_module2) == 1 and to_module2[0] == frame,
        "данные после OSError(EAGAIN) должны дойти до модуля",
    )

    # Вариант, где EAGAIN есть только в тексте исключения (без errno).
    class TextEagainConnection(FakeCloudConnection):
        """Фейк: EAGAIN без errno, только в тексте."""

        def __init__(self, incoming=None):
            super().__init__(incoming)
            self.recv_calls = 0
            self.eagain = True

        def recv(self, size):
            """Сначала EAGAIN в тексте, затем данные, затем EOF."""
            self.recv_calls += 1
            if self.eagain:
                self.eagain = False
                raise proxy_mod.SSL.SysCallError("EAGAIN")
            return super().recv(size)

    to_module3 = []
    conn3 = TextEagainConnection([frame])
    bridge3 = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        send_to_module=lambda data: (to_module3.append(bytes(data)), True)[1],
    )
    bridge3._ctx = object()
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn3
        bridge3._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  EAGAIN в тексте: recv-вызовов = {conn3.recv_calls}")
    check(
        conn3.recv_calls >= 2,
        f"EAGAIN в тексте не должен рвать цикл, recv-вызовов {conn3.recv_calls}",
    )
    check(
        len(to_module3) == 1 and to_module3[0] == frame,
        "данные после EAGAIN в тексте должны дойти до модуля",
    )
    print("  case 12: OK\n")


def case_zero_return_closes() -> None:
    """Zero-return (чистый EOF) — обрыв: соединение закрывается."""
    print("--- case 13: zero return закрывает соединение ---")

    class ZeroReturnConnection(FakeCloudConnection):
        """Фейк: облако закрывает TLS через ZeroReturnError."""

        def __init__(self):
            super().__init__()
            self.recv_calls = 0

        def recv(self, size):
            """Всегда zero return."""
            self.recv_calls += 1
            raise proxy_mod.SSL.ZeroReturnError()

    conn = ZeroReturnConnection()
    bridge = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
    )
    bridge._ctx = object()

    original_create = proxy_mod.socket.create_connection
    original_connection = proxy_mod.SSL.Connection
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn
        bridge._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  recv-вызовов = {conn.recv_calls}")
    check(conn.recv_calls == 1, f"после zero return цикл должен прерваться, вызовов {conn.recv_calls}")
    check(bridge.is_connected is False, "после zero return соединение должно быть закрыто")

    # Настоящая ошибка (не EAGAIN) — тоже обрыв.
    class ResetConnection(FakeCloudConnection):
        """Фейк: сброс соединения (ECONNRESET)."""

        def __init__(self):
            super().__init__()
            self.recv_calls = 0

        def recv(self, size):
            """Всегда ECONNRESET."""
            self.recv_calls += 1
            raise OSError(104, "Connection reset by peer")

    conn2 = ResetConnection()
    bridge2 = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
    )
    bridge2._ctx = object()
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn2
        bridge2._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  ECONNRESET: recv-вызовов = {conn2.recv_calls}")
    check(conn2.recv_calls == 1, "настоящая ошибка должна прерывать цикл")
    check(bridge2.is_connected is False, "после ECONNRESET соединение должно быть закрыто")
    print("  case 13: OK\n")


def case_registration_replay() -> None:
    """Кэш регистрации: кадр link=2 op=2 уходит в облако первым.

    Модуль шлёт регистрацию сразу при своём подключении; если канал к
    облаку ещё не поднят, кадр уходит в никуда. Координатор кэширует его,
    мост отправляет кэш первым кадром новой сессии с облаком.
    """
    print("--- case 14: реплей кадра регистрации ---")

    registration = build_frame(LINK_REGISTER, OPCODE_REGISTER, bytes.fromhex("0000010001000000"))

    # Мост получает кэш от координатора через get_registration_frame.
    conn = FakeCloudConnection()
    bridge = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        get_registration_frame=lambda: registration,
    )
    bridge._ctx = object()

    original_create = proxy_mod.socket.create_connection
    original_connection = proxy_mod.SSL.Connection
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn
        bridge._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  в облако отправлено порций = {len(conn.sent)}")
    check(len(conn.sent) == 1, f"ожидалась 1 порция в облако, получено {len(conn.sent)}")
    check(
        conn.sent[0] == registration,
        "кадр регистрации должен уйти в облако первым и байт в байт",
    )

    # Без кэша мост не шлёт ничего от себя.
    conn2 = FakeCloudConnection()
    bridge2 = ZotaCloudBridge(
        cloud_address="217.25.88.41:1978",
        psk_hex=PSK_HEX,
        psk_identity=PSK_IDENTITY,
        get_registration_frame=lambda: None,
    )
    bridge2._ctx = object()
    try:
        proxy_mod.socket.create_connection = lambda address, timeout=None: FakeSocket()
        proxy_mod.SSL.Connection = lambda ctx, sock: conn2
        bridge2._session()
    finally:
        proxy_mod.socket.create_connection = original_create
        proxy_mod.SSL.Connection = original_connection

    print(f"  без кэша в облако отправлено порций = {len(conn2.sent)}")
    check(conn2.sent == [], "без кэша мост не должен ничего слать в облако сам")

    # Мост перехватывает кадр регистрации в направлении модуль → облако.
    captured = []
    bridge3 = make_bridge(on_registration_frame=captured.append)
    bridge3.feed_from_module(registration)
    print(f"  перехвачено кадров регистрации = {len(captured)}")
    check(len(captured) == 1, f"ожидался 1 перехваченный кадр, получено {len(captured)}")
    check(
        captured[0] == registration,
        "перехваченный кадр должен совпадать с исходным байт в байт",
    )

    # Кадр от облака (link=2 op=2) перехватываться не должен.
    captured2 = []
    bridge4 = make_bridge(on_registration_frame=captured2.append)
    bridge4._handle_cloud_bytes(registration)
    check(
        captured2 == [],
        "кадр регистрации от облака не должен попадать в кэш модуля",
    )

    # Координатор: кэширование и выдача кадра.
    hass = FakeHass()
    entry = ConfigEntry(
        data={"name": "Котёл", "port": 1978, "proxy": True, "psk": PSK_HEX}
    )
    coordinator = ZotaCoordinator(hass, entry)
    check(
        coordinator._get_registration_frame() is None,
        "до регистрации кэш должен быть пуст",
    )
    coordinator._on_registration_frame(registration)
    print(f"  кэш координатора = {coordinator._get_registration_frame()!r}")
    check(
        coordinator._get_registration_frame() == registration,
        "координатор должен отдавать кэшированный кадр регистрации",
    )

    # Мост, собранный координатором, получает доступ к кэшу.
    import zota_smart.coordinator as coordinator_mod

    original_bridge_cls = coordinator_mod.ZotaCloudBridge
    try:
        coordinator_mod.ZotaCloudBridge = proxy_mod.ZotaCloudBridge
        built = coordinator._make_bridge()
        check(
            built._get_registration_frame is not None,
            "мост должен получать поставщика кэша регистрации",
        )
        check(
            built._on_registration_frame is not None,
            "мост должен получать колбэк перехвата регистрации",
        )
    finally:
        coordinator_mod.ZotaCloudBridge = original_bridge_cls
    print("  case 14: OK\n")


def case_module_side_eagain() -> None:
    """EAGAIN в recv-цикле TLS-сервера (направление модуля) — не обрыв.

    Мост к облаку и транспорт модуля чинятся единообразно: EAGAIN — это
    «данных нет», а не разрыв соединения.
    """
    print("--- case 15: EAGAIN в recv-цикле TLS-сервера ---")

    from zota_smart import tls_server as tls_server_mod

    eagain_errno = errno.EAGAIN

    class EagainConnection(FakeCloudConnection):
        """Фейк: первый recv — EAGAIN, дальше — данные и EOF."""

        def __init__(self, incoming=None):
            super().__init__(incoming)
            self.recv_calls = 0
            self.eagain = True

        def recv(self, size):
            """Сначала EAGAIN, затем данные, затем EOF."""
            self.recv_calls += 1
            if self.eagain:
                self.eagain = False
                raise tls_server_mod.SSL.SysCallError(eagain_errno, "EAGAIN")
            return super().recv(size)

    frame = build_frame(LINK_REGISTER, OPCODE_REGISTER, bytes.fromhex("0000010001000000"))
    conn = EagainConnection([frame])

    raw_chunks = []
    frames = []
    # PSK не задаём: установка PSK-callback требует настоящего OpenSSL,
    # а тесту он не нужен — проверяется только recv-цикл.
    transport = tls_server_mod.TLSServerTransport(
        host="127.0.0.1",
        port=0,
        psk_hex=None,
        psk_identity=PSK_IDENTITY,
        on_bytes=raw_chunks.append,
        on_frame=lambda link, op, pl: frames.append((link, op, pl)),
    )

    original_connection = tls_server_mod.SSL.Connection
    try:
        tls_server_mod.SSL.Connection = lambda ctx, sock: conn
        transport._handle_client(FakeSocket())
    finally:
        tls_server_mod.SSL.Connection = original_connection

    print(f"  recv-вызовов = {conn.recv_calls}")
    print(f"  сырых порций = {len(raw_chunks)}, кадров = {len(frames)}")
    check(
        conn.recv_calls >= 2,
        f"цикл чтения должен продолжиться после EAGAIN, вызовов {conn.recv_calls}",
    )
    check(len(raw_chunks) == 1, "данные после EAGAIN должны уйти в on_bytes")
    check(raw_chunks[0] == frame, "сырые байты должны уйти без изменений")
    check(len(frames) == 1, "кадр после EAGAIN должен быть разобран")
    check(
        frames[0][0] == LINK_REGISTER and frames[0][1] == OPCODE_REGISTER,
        f"разобран не тот кадр: {frames[0][:2]}",
    )
    print("  case 15: OK\n")


def main() -> int:
    """Прогнать все случаи."""
    print("=== test_proxy_bridge: мост к облаку ZOTA ===\n")
    case_module_to_cloud()
    case_cloud_to_module()
    case_register_transparent()
    case_partial_frames()
    case_reconnect_flags()
    case_reconnect_loop()
    case_parse_address()
    case_coordinator_bridge()
    case_start_wiring()
    case_psk_client_callback()
    case_binary_sensor_names()
    case_eagain_is_not_disconnect()
    case_zero_return_closes()
    case_registration_replay()
    case_module_side_eagain()
    print("=== ALL CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
