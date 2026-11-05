"""TLS-PSK сервер для канала ZOTA Smart :1978 (PROTOCOL.md §11.2).

Транспорт: TLS 1.2, ciphersuite ``TLS_PSK_WITH_AES_128_CBC_SHA256``
(OpenSSL ``0x00AE``), identity ``"Smart key"``, 32-байтовый PSK.

Реализация на pyOpenSSL + обычный сокет в отдельном потоке-демоне.
Модуль ничего не знает про Home Assistant и asyncio: колбэки вызываются
напрямую из потока сервера, вызывающий код сам отвечает за потокобезопасность
(в HA-интеграции это будет ``hass.loop.call_soon_threadsafe``).

Протокол кадров разбирает :mod:`.protocol` — модуль намеренно зависит только
от pyOpenSSL и стандартной библиотеки.

Почему PSK-callback ставится в обход pyOpenSSL: в библиотеке нет ни
``set_psk_server_callback``, ни ``set_psk_client_callback`` (проверено на
pyOpenSSL 23.0.0, 24.0.0, 25.0.0, 26.0.0 и 26.4.0). Живой факт из
HA-контейнера (2026-11-04): ``AttributeError: 'Context' object has no
attribute 'set_psk_server_callback'``. Поэтому C-функция
``SSL_CTX_set_psk_server_callback`` вызывается напрямую по указателю
``SSL_CTX``, который pyOpenSSL держит в приватном поле ``Context._context``.

Чем именно вызывается — важный момент. Вызов через ``ctypes.CDLL`` по
имени ``libssl.so.3`` НЕБЕЗОПАСЕН: ``cryptography`` (а на нём построен
pyOpenSSL) начиная с ветки на Rust-биндингах тянет OpenSSL статически
(в manylinux-колёсах нет зависимости от ``libssl.so``), поэтому
``ctypes.CDLL("libssl.so.3")`` загрузит СИСТЕМНЫЙ OpenSSL — другой экземпляр
библиотеки, чем тот, который выделил ``SSL_CTX``. Вызов функции из чужого
экземпляра на чужом ``SSL_CTX`` — undefined behavior (воспроизведено: SIGSEGV
на рукопожатии).

Поэтому колбэк ставится через собственные bindings ``cryptography``:
``cryptography.hazmat.bindings.openssl.binding.Binding().lib`` — это тот же
экземпляр OpenSSL, которому принадлежит ``SSL_CTX``, и в нём есть символ
``SSL_CTX_set_psk_server_callback`` (в cryptography он помечен флагом
``Cryptography_HAS_PSK``). Callback создаётся через ``ffi.callback(...)``.

Нюанс указателя: поле ``Context._context`` — это CFFI-cdata, а не число, так
что ``ctypes.c_void_p(ctx._context)`` невозможен (``TypeError: cannot be
converted to pointer``). В CFFI-API режиме указатель передаётся как есть —
``lib.SSL_CTX_set_psk_server_callback(ctx._context, callback)``.

Целевое окружение: OpenSSL 3.x (в HA-контейнере — OpenSSL 3.5.4,
CPython 3.14.2).
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, Optional

from OpenSSL import SSL

try:  # внутри пакета интеграции
    from .protocol import IDLE_SLEEP, FrameBuffer, build_frame, is_idle_error, log_frame
except ImportError:  # standalone-режим: protocol.py лежит рядом
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from protocol import IDLE_SLEEP, FrameBuffer, build_frame, is_idle_error, log_frame  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

#: Ciphersuite, зафиксированный в PROTOCOL.md §11.2.
#: ``@SECLEVEL=0`` обязателен: на дефолтном уровне безопасности OpenSSL
#: отвергает PSK-сьюит.
CIPHER_LIST = "@SECLEVEL=0:PSK-AES128-CBC-SHA256"

#: Пауза перед новым accept после обрыва соединения, с.
RECONNECT_DELAY = 10.0

#: Размер порции recv, байт.
RECV_SIZE = 4096

#: Таймаут неблокирующего accept/listen, с.
ACCEPT_TIMEOUT = 1.0

#: Сколько раз запись клиенту может подряд упереться в «сокет не готов»
#: (EAGAIN / WantWrite), прежде чем она будет признана неудачной.
WRITE_IDLE_LIMIT = 50

#: Имя OpenSSL-библиотеки в логе установки callback. Используется та
#: библиотека, что вшита в ``cryptography`` (в HA-контейнере — ``libssl.so.3``,
#: OpenSSL 3.5.4); вручную через ctypes она не подгружается — см. докстринг
#: модуля про разные экземпляры OpenSSL.
_LIBSSL_NAMES = ("libssl.so.3", "libssl.so", "ssl")

#: C-сигнатура серверного PSK-callback:
#: ``unsigned int (*)(SSL *ssl, const char *identity, unsigned char *psk,
#: unsigned int max_psk_len)``.
_PSK_CB_SIGNATURE = "unsigned int(SSL *, const char *, unsigned char *, unsigned int)"


def _load_openssl_binding():
    """Взять bindings OpenSSL, которыми пользуется сам pyOpenSSL.

    Возвращает пару ``(ffi, lib)`` из
    ``cryptography.hazmat.bindings.openssl.binding.Binding``.

    Важно: это bindings *того же* экземпляра OpenSSL, который выделил
    ``SSL_CTX``. Подгрузка libssl через ``ctypes`` дала бы другой
    экземпляр библиотеки (cryptography тянет OpenSSL статически) — вызов
    функции из чужого экземпляра на чужом ``SSL_CTX`` не определён.
    """
    try:
        from cryptography.hazmat.bindings.openssl.binding import Binding
    except ImportError as err:
        raise RuntimeError(
            "Не удалось импортировать bindings OpenSSL из cryptography: %s" % err
        ) from err

    binding = Binding()
    return binding.ffi, binding.lib


def _install_psk_callback(
    ctx: SSL.Context,
    psk_hex: str,
    psk_identity: str,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Установить серверный PSK-callback в ``SSL_CTX``.

    pyOpenSSL не даёт доступа к серверному PSK-callback, поэтому C-функция
    ``SSL_CTX_set_psk_server_callback`` вызывается напрямую через bindings
    ``cryptography`` — того же экземпляра OpenSSL, которому принадлежит
    ``SSL_CTX``.

    Возвращает ``True`` при успешной установке.
    """
    log = logger or _LOGGER

    ffi, lib = _load_openssl_binding()

    set_callback = getattr(lib, "SSL_CTX_set_psk_server_callback", None)
    if set_callback is None:
        raise RuntimeError(
            "В OpenSSL из cryptography нет символа SSL_CTX_set_psk_server_callback "
            "(сборка без поддержки PSK)"
        )

    psk_bytes = bytes.fromhex(psk_hex)
    expected_identity = psk_identity.encode()

    @ffi.callback(_PSK_CB_SIGNATURE)
    def _psk_server_callback(ssl, identity, psk_out, max_psk_len):
        """Отдать PSK только для известной identity, иначе — 0 (отказ)."""
        if identity == ffi.NULL:
            log.warning("TLS-PSK: пустая identity, отклонено")
            return 0
        if ffi.string(identity) != expected_identity:
            log.warning(
                "TLS-PSK: неизвестная identity %r, отклонено", ffi.string(identity)
            )
            return 0
        if len(psk_bytes) > max_psk_len:
            log.warning(
                "TLS-PSK: буфер PSK мал (%d < %d), отклонено",
                max_psk_len,
                len(psk_bytes),
            )
            return 0
        ffi.buffer(psk_out, len(psk_bytes))[:] = psk_bytes
        return len(psk_bytes)

    try:
        ctx_ptr = ctx._context  # type: ignore[attr-defined]
    except AttributeError as err:
        raise RuntimeError(
            "pyOpenSSL сменил приватный API _context: не удалось получить "
            "указатель SSL_CTX для установки PSK-callback"
        ) from err

    set_callback(ctx_ptr, _psk_server_callback)

    # Ссылку на C-колбэк держим на самом контексте намеренно: без живой
    # ссылки CPython может собрать cffi-callback, и OpenSSL получит висячий
    # указатель на освобождённый callback — рукопожатие упадёт или уедет
    # в случайный код.
    ctx._zota_psk_callback = _psk_server_callback  # type: ignore[attr-defined]

    log.info(
        "PSK server callback installed via ctypes (libssl %s, cryptography bindings)",
        _LIBSSL_NAMES[0],
    )
    return True


class TLSServerTransport:
    """TLS-PSK сервер поверх TCP, один клиент за раз.

    Жизненный цикл:

    1. :meth:`start` поднимает поток-демон: bind → listen → accept.
    2. Рукопожатие TLS-PSK; при успехе — колбэк ``on_client``.
    3. recv-цикл: байты → :class:`protocol.FrameBuffer` → целые кадры →
       колбэк ``on_frame(link, opcode, payload)``.
    4. При обрыве — колбэк ``on_disconnect`` и возврат в accept (с паузой
       :data:`RECONNECT_DELAY`).

    Потокобезопасность: ``send_frame`` и ``send_bytes`` можно вызывать из
    любого потока (запись защищена внутренним lock). Остальные колбэки
    приходят в потоке сервера — вызывающий код обязан сам обеспечить
    безопасность, если колбэк трогает состояние, разделяемое с другими
    потоками.

    В режиме PROXY (``on_bytes`` задан) транспорт отдаёт сырые байты ещё и
    в ``on_bytes`` — мост к облаку (:mod:`.proxy`) пересылает их дословно.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 1978,
        psk_hex: Optional[str] = None,
        psk_identity: str = "Smart key",
        on_client: Optional[Callable[..., None]] = None,
        on_frame: Optional[Callable[[int, int, bytes], None]] = None,
        on_disconnect: Optional[Callable[..., None]] = None,
        on_bytes: Optional[Callable[[bytes], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Создать транспорт (сокет в конструкторе не открывается)."""
        self._host = host
        self._port = port
        self._psk_hex = psk_hex
        self._psk_identity = psk_identity
        self._on_client = on_client
        self._on_frame = on_frame
        self._on_disconnect = on_disconnect
        self._on_bytes = on_bytes
        self._log = logger or _LOGGER

        self._ctx = self._build_context()

        self._listener: Optional[socket.socket] = None
        self._conn: Optional[SSL.Connection] = None
        self._sock: Optional[socket.socket] = None
        self._frame_buffer = FrameBuffer()
        self._send_lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()

    # --- Конструктор контекста ------------------------------------------

    def _build_context(self) -> SSL.Context:
        """Собрать TLS-контекст сервера с PSK-callback."""
        ctx = SSL.Context(SSL.TLS_SERVER_METHOD)
        ctx.set_cipher_list(CIPHER_LIST)
        # PROTOCOL.md §11.2 — только TLS 1.2: в TLS 1.3 сьют PSK выбирается
        # иначе, и зафиксированный TLS_PSK_WITH_AES_128_CBC_SHA256 не
        # предлагается. Без пина клиент с TLS 1.3 не соединится.
        ctx.set_min_proto_version(SSL.TLS1_2_VERSION)
        ctx.set_max_proto_version(SSL.TLS1_2_VERSION)
        if not self._psk_hex:
            self._log.error("TLS-PSK: PSK не задан, рукопожатие невозможно")
            return ctx
        _install_psk_callback(ctx, self._psk_hex, self._psk_identity, self._log)
        return ctx

    # --- Публичный API ----------------------------------------------------

    @property
    def port(self) -> int:
        """Порт, на котором слушает транспорт."""
        return self._port

    @property
    def is_connected(self) -> bool:
        """Есть ли сейчас установленное TLS-соединение."""
        return self._connected.is_set()

    def start(self) -> None:
        """Запустить поток-демон сервера."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serve, name="zota-tls-server", daemon=True
        )
        self._thread.start()
        self._log.info("TLS-PSK сервер запущен: %s:%d", self._host, self._port)

    def stop(self, timeout: float = 5.0) -> None:
        """Остановить сервер: закрыть listener/соединение и дождаться потока."""
        self._stop_event.set()
        self._close_connection()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                self._log.warning("Поток TLS-сервера не завершился за %.1f c", timeout)
        self._thread = None

    def send_frame(self, link: int, opcode: int, payload: bytes = b"") -> bool:
        """Отправить кадр клиенту.

        Вызывается из любого потока. Возвращает False, если соединения нет
        или запись не удалась.
        """
        data = build_frame(link, opcode, payload)
        return self._send_raw(data)

    def send_bytes(self, data: bytes) -> bool:
        """Отправить клиенту сырые байты, без пересборки в кадр.

        Нужен режиму PROXY (:mod:`.proxy`): облачные кадры уходят в модуль
        дословно — вместе с хвостами keepalive-кадров (``op=4``, +10 байт),
        которые ``build_frame`` собрать не может.

        Вызывается из любого потока.
        """
        return self._send_raw(bytes(data))

    def _send_raw(self, data: bytes) -> bool:
        """Записать байты в TLS-соединение с retry на SSLWantWrite."""
        with self._send_lock:
            conn = self._conn
            if conn is None:
                return False
            log_frame("H2M", data)
            offset = 0
            idle_rounds = 0
            while offset < len(data):
                try:
                    written = conn.send(data[offset:])
                except SSL.WantWriteError:
                    idle_rounds += 1
                    if idle_rounds > WRITE_IDLE_LIMIT:
                        self._log.warning("TLS-запись: сокет не готов писать")
                        return False
                    self._idle_wait()
                    continue
                except SSL.WantReadError:
                    # Сервер не должен читать во время записи; ждём
                    # готовности сокета к записи.
                    idle_rounds += 1
                    if idle_rounds > WRITE_IDLE_LIMIT:
                        self._log.warning("TLS-запись: сокет не готов читать")
                        return False
                    self._idle_wait()
                    continue
                except (SSL.Error, OSError) as err:
                    if is_idle_error(err):
                        # EAGAIN при записи — «данных нет», а не обрыв.
                        idle_rounds += 1
                        if idle_rounds > WRITE_IDLE_LIMIT:
                            self._log.warning("TLS-запись: сокет не готов писать")
                            return False
                        self._idle_wait()
                        continue
                    self._log.warning("TLS-запись не удалась: %s", err)
                    return False
                idle_rounds = 0
                if written <= 0:
                    return False
                offset += written
        return True

    # --- Поток сервера ----------------------------------------------------

    def _serve(self) -> None:
        """Основной цикл: accept → handshake → recv → disconnect → repeat."""
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(ACCEPT_TIMEOUT)
            listener.bind((self._host, self._port))
            listener.listen(1)
            self._log.info("TLS-PSK слушает %s:%d", self._host, self._port)
        except OSError as err:
            self._log.error("Не удалось открыть сокет %s:%d: %s", self._host, self._port, err)
            self._listener = None
            return

        while not self._stop_event.is_set():
            try:
                sock, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                self._log.warning("accept() не удался")
                continue

            self._log.info("Клиент подключился: %s", addr)
            try:
                self._handle_client(sock)
            except Exception:  # noqa: BLE001 — сервер не должен падать от клиента
                self._log.exception("Ошибка при обслуживании клиента")
            finally:
                self._close_connection()
                self._frame_buffer.reset()
                if self._on_disconnect is not None:
                    try:
                        self._on_disconnect()
                    except Exception:  # noqa: BLE001
                        self._log.exception("Колбэк on_disconnect упал")

            if not self._stop_event.is_set():
                self._log.info("Ожидание %d c перед новым accept", int(RECONNECT_DELAY))
                if self._stop_event.wait(RECONNECT_DELAY):
                    break

        try:
            listener.close()
        except OSError:
            pass
        self._listener = None
        self._log.info("TLS-PSK сервер остановлен")

    def _handle_client(self, sock: socket.socket) -> None:
        """Рукопожатие и recv-цикл для одного клиента."""
        sock.settimeout(None)
        conn = SSL.Connection(self._ctx, sock)
        # pyOpenSSL не выводит роль из контекста: без явного accept-state
        # рукопожатие падает с "connection type not set".
        conn.set_accept_state()
        self._sock = sock
        self._conn = conn
        self._frame_buffer.reset()

        try:
            self._do_handshake(conn)
        except (SSL.Error, OSError) as err:
            self._log.warning("TLS-рукопожатие не удалось: %s", err)
            return

        self._connected.set()
        self._log.info("TLS-рукопожатие завершено")
        if self._on_client is not None:
            try:
                self._on_client()
            except Exception:  # noqa: BLE001
                self._log.exception("Колбэк on_client упал")

        try:
            while not self._stop_event.is_set():
                try:
                    data = conn.recv(RECV_SIZE)
                except SSL.WantReadError:
                    # Данных нет — это не обрыв: ждём и продолжаем цикл.
                    self._idle_wait()
                    continue
                except SSL.WantWriteError:
                    self._idle_wait()
                    continue
                except SSL.ZeroReturnError:
                    self._log.info("Клиент закрыл TLS-соединение (zero return)")
                    break
                except (SSL.Error, OSError) as err:
                    if is_idle_error(err):
                        # EAGAIN на неблокирующем сокете — «данных нет»,
                        # а не обрыв соединения.
                        self._idle_wait()
                        continue
                    if not self._stop_event.is_set():
                        self._log.info("Соединение закрыто: %s", err)
                    break
                if not data:
                    self._log.info("Клиент разорвал соединение (EOF)")
                    break
                log_frame("M2H", data)
                self._feed_raw(data)
                self._feed(data)
        finally:
            self._connected.clear()

    def _do_handshake(self, conn: SSL.Connection) -> None:
        """Рукопожатие с обработкой SSLWantRead/SSLWantWrite."""
        while True:
            try:
                conn.do_handshake()
                return
            except SSL.WantReadError:
                # Данных нет — ждём и продолжаем, а не крутим цикл вхолостую.
                self._idle_wait()
                continue
            except SSL.WantWriteError:
                self._idle_wait()
                continue

    def _idle_wait(self) -> None:
        """Пауза при отсутствии данных (EAGAIN/WantRead).

        Короткая пауза вместо мгновенного повтора: сокет в неблокирующем
        режиме возвращает EAGAIN немедленно, и без паузы цикл чтения
        крутился бы вхолостую.
        """
        if self._stop_event.wait(IDLE_SLEEP):
            return

    def _feed_raw(self, data: bytes) -> None:
        """Отдать сырые байты колбэку ``on_bytes`` (мост к облаку)."""
        if self._on_bytes is None:
            return
        try:
            self._on_bytes(data)
        except Exception:  # noqa: BLE001
            self._log.exception("Колбэк on_bytes упал")

    def _feed(self, data: bytes) -> None:
        """Разобрать пришедшие байты на кадры и вызвать on_frame."""
        try:
            frames = self._frame_buffer.feed(data)
        except ValueError as err:
            self._log.warning("Некорректный кадр: %s", err)
            self._frame_buffer.reset()
            return
        for frame in frames:
            self._log.debug(
                "RX link=%d opcode=%d len=%d",
                frame.link,
                frame.opcode,
                len(frame.payload),
            )
            if self._on_frame is None:
                continue
            try:
                self._on_frame(frame.link, frame.opcode, frame.payload)
            except Exception:  # noqa: BLE001
                self._log.exception("Колбэк on_frame упал")

    def _close_connection(self) -> None:
        """Закрыть активное TLS-соединение и сокет."""
        self._connected.clear()
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.shutdown()
            except (SSL.Error, OSError):
                pass
            try:
                conn.close()
            except (SSL.Error, OSError):
                pass
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


__all__ = [
    "CIPHER_LIST",
    "RECONNECT_DELAY",
    "TLSServerTransport",
    "_install_psk_callback",
    "_load_openssl_binding",
]
