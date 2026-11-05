"""Мост к облаку ZOTA (режим PROXY): TLS-PSK-клиент + прозрачный форвардинг.

Задача модуля — быть «облаком» для LAN-модуля котла: принимать его
TLS-сессию (это делает :class:`.tls_server.TLSServerTransport`) и
параллельно держать свою сессию с настоящим облаком ZOTA
(``217.25.88.41:1978``), перебрасывая байты в обе стороны.

Мост **прозрачен**: кадры модуля (включая регистрацию ``link=2 op=2``)
уходят в облако как есть — облако само узнаёт котёл, мост ничего не
подменяет и не генерирует. Обратно облако присылает свои write'ы и блобы
(например конфиг-блоб насоса 2, 512 байт) — они так же уходят в модуль
дословно.

Два отступления от «полной прозрачности», оба — по необходимости:

* ``EAGAIN``/``WantRead`` в цикле чтения — это «данных нет», а не обрыв.
  Соединение закрывается только по zero-return (чистый EOF) или настоящей
  ошибке (``ECONNRESET`` и т. п.).
* Кадр регистрации ``link=2 op=2`` кэшируется координатором и отправляется
  **первым** в новой сессии с облаком: модуль шлёт регистрацию сразу при
  своём подключении, и если канал к облаку ещё не поднят, кадр уходит
  в никуда — облако не видит серийник и рвёт «молчаливую» сессию по
  таймауту.

Параллельно обе стороны раскладываются на кадры
(:class:`.protocol.FrameBuffer` — по буферу на направление, см. пояснение
в ``ZotaCloudBridge.__init__``), и оба потока кадров кормят один и тот же
``on_frame`` координатора: состояние Home Assistant обновляется локально —
в том числе когда write пришёл из облака.

TLS-PSK на стороне клиента ставится так же, как на стороне сервера
(см. :mod:`.tls_server`): pyOpenSSL не даёт доступа к PSK-callback'ам
(ни ``set_psk_client_callback``, ни ``set_psk_server_callback`` — проверено
на pyOpenSSL 23…26), поэтому C-функция
``SSL_CTX_set_psk_client_callback`` вызывается напрямую через bindings
``cryptography`` — того же экземпляра OpenSSL, которому принадлежит
``SSL_CTX``. Вызов через ``ctypes.CDLL("libssl.so.3")`` был бы
небезопасен: ``cryptography`` тянет OpenSSL статически, и вызов функции из
чужого экземпляра библиотеки на чужом ``SSL_CTX`` — undefined behavior
(воспроизведено: SIGSEGV на рукопожатии).

Сигнатура клиентского PSK-callback (OpenSSL 3.x, ``ssl.h``)::

    unsigned int (*SSL_psk_client_cb_func)(SSL *ssl, const char *hint,
        char *identity, unsigned int max_identity_len,
        unsigned char *psk, unsigned int max_psk_len);

Колбэк обязан записать identity в ``identity`` и PSK в ``psk``, вернув
число записанных байт PSK; ``0`` означает отказ от рукопожатия.
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

#: Ciphersuite, зафиксированный в PROTOCOL.md §11.2. ``@SECLEVEL=0``
#: обязателен: на дефолтном уровне безопасности OpenSSL отвергает
#: PSK-сьюит.
CIPHER_LIST = "@SECLEVEL=0:PSK-AES128-CBC-SHA256"

#: Пауза перед следующей попыткой подключения к облаку, с.
RECONNECT_DELAY = 10.0

#: Размер порции recv из облака, байт.
RECV_SIZE = 4096

#: Таймаут подключения к облаку (connect + рукопожатие), с.
CONNECT_TIMEOUT = 15.0

#: Сколько раз запись в облако может подряд упереться в «сокет не готов»
#: (EAGAIN / WantWrite), прежде чем она будет признана неудачной.
#: Ограничение нужно, чтобы зависший сокет не держал блокировку записи
#: бесконечно.
WRITE_IDLE_LIMIT = 50

#: link=2 — кадры регистрации модуля (§11.3). Значение продублировано из
#: ``const.py`` — мост намеренно не зависит от пакета интеграции.
LINK_REGISTER = 2

#: opcode=2 — регистрация (link=2).
OPCODE_REGISTER = 2

#: C-сигнатура клиентского PSK-callback (см. докстринг модуля).
_PSK_CLIENT_CB_SIGNATURE = (
    "unsigned int(SSL *, const char *, char *, unsigned int, unsigned char *, unsigned int)"
)


def _load_openssl_binding():
    """Взять bindings OpenSSL, которыми пользуется сам pyOpenSSL.

    Возвращает пару ``(ffi, lib)`` из
    ``cryptography.hazmat.bindings.openssl.binding.Binding``.

    Это bindings *того же* экземпляра OpenSSL, который выделил ``SSL_CTX``.
    Подгрузка libssl через ``ctypes`` дала бы другой экземпляр библиотеки
    (cryptography тянет OpenSSL статически) — вызов функции из чужого
    экземпляра на чужом ``SSL_CTX`` не определён.
    """
    try:
        from cryptography.hazmat.bindings.openssl.binding import Binding
    except ImportError as err:
        raise RuntimeError(
            "Не удалось импортировать bindings OpenSSL из cryptography: %s" % err
        ) from err

    binding = Binding()
    return binding.ffi, binding.lib


def _install_psk_client_callback(
    ctx: SSL.Context,
    psk_hex: str,
    psk_identity: str,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Установить клиентский PSK-callback в ``SSL_CTX``.

    pyOpenSSL не даёт доступа к ``SSL_CTX_set_psk_client_callback``, поэтому
    C-функция вызывается напрямую через bindings ``cryptography`` — того же
    экземпляра OpenSSL, которому принадлежит ``SSL_CTX``.

    Возвращает ``True`` при успешной установке.
    """
    log = logger or _LOGGER

    ffi, lib = _load_openssl_binding()

    set_callback = getattr(lib, "SSL_CTX_set_psk_client_callback", None)
    if set_callback is None:
        raise RuntimeError(
            "В OpenSSL из cryptography нет символа SSL_CTX_set_psk_client_callback "
            "(сборка без поддержки PSK)"
        )

    psk_bytes = bytes.fromhex(psk_hex)
    identity_bytes = psk_identity.encode()

    @ffi.callback(_PSK_CLIENT_CB_SIGNATURE)
    def _psk_client_callback(ssl, hint, identity_out, max_identity_len, psk_out, max_psk_len):
        """Отдать identity и PSK облаку; ``0`` — отказ от рукопожатия."""
        if len(identity_bytes) + 1 > max_identity_len:
            log.warning(
                "TLS-PSK: буфер identity мал (%d < %d), отклонено",
                max_identity_len,
                len(identity_bytes) + 1,
            )
            return 0
        if len(psk_bytes) > max_psk_len:
            log.warning(
                "TLS-PSK: буфер PSK мал (%d < %d), отклонено",
                max_psk_len,
                len(psk_bytes),
            )
            return 0

        # identity копируется с завершающим NUL — этого ждёт OpenSSL.
        ffi.buffer(identity_out, len(identity_bytes) + 1)[:] = identity_bytes + b"\x00"
        ffi.buffer(psk_out, len(psk_bytes))[:] = psk_bytes
        return len(psk_bytes)

    try:
        ctx_ptr = ctx._context  # type: ignore[attr-defined]
    except AttributeError as err:
        raise RuntimeError(
            "pyOpenSSL сменил приватный API _context: не удалось получить "
            "указатель SSL_CTX для установки PSK-callback"
        ) from err

    set_callback(ctx_ptr, _psk_client_callback)

    # Ссылку на C-колбэк держим на самом контексте намеренно: без живой
    # ссылки CPython может собрать cffi-callback, и OpenSSL получит висячий
    # указатель на освобождённый callback — рукопожатие упадёт или уедет
    # в случайный код.
    ctx._zota_psk_client_callback = _psk_client_callback  # type: ignore[attr-defined]

    log.info("PSK client callback установлен через cryptography bindings")
    return True


def parse_cloud_address(address: str, default_port: int = 1978) -> tuple[str, int]:
    """Разобрать адрес облака ``host:port`` в ``(host, port)``.

    Порт не указан — берётся ``default_port``. IPv6 в скобках
    (``[::1]:1978``) поддерживается.
    """
    text = (address or "").strip()
    if not text:
        raise ValueError("адрес облака не задан")
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("незакрытая скобка в адресе облака: %r" % address)
        host = text[1:end]
        rest = text[end + 1 :]
        if rest.startswith(":") and rest[1:]:
            return host, int(rest[1:])
        return host, default_port
    if ":" in text:
        host, _, port = text.rpartition(":")
        if not port:
            raise ValueError("порт в адресе облака не указан: %r" % address)
        return host, int(port)
    return text, default_port


class ZotaCloudBridge:
    """Мост между LAN-модулем котла и облаком ZOTA.

    Жизненный цикл:

    1. :meth:`start` поднимает поток-демон.
    2. Поток подключается к облаку (TLS-PSK-клиент) и держит сессию.
    3. Байты модуля → облаку и обратно, прозрачно, без пересборки.
    4. При обрыве — переподключение через :data:`RECONNECT_DELAY`.

    Мост ничего не генерирует: ни poll-кадров, ни keepalive, ни ответов на
    регистрацию. Своих кадров у него нет вообще — он только пересылает
    байты. Опрос модуля и keepalive делает сам модуль (см. §11.3).

    Потокобезопасность: :meth:`stop` можно вызывать из любого потока.
    """

    def __init__(
        self,
        cloud_address: str,
        psk_hex: Optional[str] = None,
        psk_identity: str = "Smart key",
        on_frame: Optional[Callable[[int, int, bytes], None]] = None,
        send_to_module: Optional[Callable[[bytes], bool]] = None,
        get_registration_frame: Optional[Callable[[], Optional[bytes]]] = None,
        on_registration_frame: Optional[Callable[[bytes], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Создать мост (подключение к облаку в конструкторе не открывается)."""
        self._cloud_address = cloud_address
        self._psk_hex = psk_hex
        self._psk_identity = psk_identity
        self._on_frame = on_frame
        self._send_to_module = send_to_module
        self._get_registration_frame = get_registration_frame
        self._on_registration_frame = on_registration_frame
        self._log = logger or _LOGGER

        self._host, self._port = parse_cloud_address(cloud_address)

        self._ctx: Optional[SSL.Context] = None
        self._conn: Optional[SSL.Connection] = None
        self._sock: Optional[socket.socket] = None
        # Разбор кадров — по буферу на направление. Один общий буфер на оба
        # направления невозможен: TCP не сохраняет границы сообщений, и
        # полукадр от модуля, смешанный с полукадром от облака, дал бы
        # мусорные кадры. Оба буфера кормят один и тот же ``on_frame``.
        self._module_frames = FrameBuffer()
        self._cloud_frames = FrameBuffer()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        #: Синхронизация записи в облако: пишут поток моста (реплей
        #: регистрации) и поток транспорта модуля (обычный форвардинг).
        self._write_lock = threading.Lock()

    # --- Свойства ---------------------------------------------------------

    @property
    def host(self) -> str:
        """Хост облака."""
        return self._host

    @property
    def port(self) -> int:
        """Порт облака."""
        return self._port

    @property
    def is_connected(self) -> bool:
        """Есть ли сейчас установленное соединение с облаком."""
        return self._connected.is_set()

    @property
    def is_running(self) -> bool:
        """Идёт ли сейчас поток моста."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- Публичный API ----------------------------------------------------

    def start(self) -> None:
        """Запустить поток-демон моста."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="zota-cloud-bridge", daemon=True
        )
        self._thread.start()
        self._log.info(
            "Мост к облаку ZOTA запущен: %s:%d", self._host, self._port
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Остановить мост: закрыть соединение с облаком и дождаться потока."""
        self._stop_event.set()
        self._close_connection()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                self._log.warning("Поток моста к облаку не завершился за %.1f c", timeout)
        self._thread = None

    def send_to_cloud(self, data: bytes) -> bool:
        """Отправить байты в облако (прозрачно).

        Вызывается из потока транспорта модуля. Возвращает ``False``, если
        соединения с облаком нет или запись не удалась.
        """
        with self._write_lock:
            return self._write(self._conn, data)

    def feed_from_module(self, data: bytes) -> None:
        """Обработать байты от модуля: переслать в облако и разобрать локально.

        Вызывается из потока транспорта. Модуль не ждёт ответа от моста —
        поэтому, если в облако писать некуда, кадр всё равно разбирается
        локально (состояние HA обновляется), а ошибка отправки не роняет
        разбор.
        """
        log_frame("M2C", data)
        self.send_to_cloud(data)
        self._feed_frames(self._module_frames, data, "MODULE")

    # --- Внутреннее --------------------------------------------------------

    def _build_context(self) -> SSL.Context:
        """Собрать TLS-контекст клиента с PSK-callback."""
        ctx = SSL.Context(SSL.TLS_CLIENT_METHOD)
        ctx.set_cipher_list(CIPHER_LIST)
        # PROTOCOL.md §11.2 — только TLS 1.2: в TLS 1.3 сьют PSK выбирается
        # иначе, и зафиксированный TLS_PSK_WITH_AES_128_CBC-SHA256 не
        # предлагается. Без пина клиент с TLS 1.3 не соединится.
        ctx.set_min_proto_version(SSL.TLS1_2_VERSION)
        ctx.set_max_proto_version(SSL.TLS1_2_VERSION)
        if not self._psk_hex:
            self._log.error("TLS-PSK: PSK не задан, рукопожатие невозможно")
            return ctx
        _install_psk_client_callback(
            ctx, self._psk_hex, self._psk_identity, self._log
        )
        return ctx

    def _run(self) -> None:
        """Основной цикл моста: подключение → проксирование → реконнект."""
        if self._ctx is None:
            self._ctx = self._build_context()
        while not self._stop_event.is_set():
            try:
                self._session()
            except Exception:  # noqa: BLE001 — мост не должен падать
                if not self._stop_event.is_set():
                    self._log.exception("Ошибка сессии с облаком ZOTA")
            finally:
                self._close_connection()
            if self._stop_event.is_set():
                break
            self._log.warning(
                "Соединение с облаком ZOTA потеряно, переподключение через %d c",
                int(RECONNECT_DELAY),
            )
            if self._stop_event.wait(RECONNECT_DELAY):
                break
        self._log.info("Мост к облаку ZOTA остановлен")

    def _session(self) -> None:
        """Одна сессия с облаком: рукопожатие и проксирование байтов."""
        sock = socket.create_connection((self._host, self._port), timeout=CONNECT_TIMEOUT)
        self._sock = sock
        conn = SSL.Connection(self._ctx, sock)
        conn.set_connect_state()
        self._conn = conn
        # Буфер облака сбрасывается: новая сессия — новый поток байтов.
        # Буфер модуля не трогаем: поток модуля от переподключения облака
        # не зависит, и полукадр в нём не должен потеряться.
        self._cloud_frames.reset()

        # Первым делом в новой сессии — кэшированный кадр регистрации
        # модуля (link=2 op=2). Модуль шлёт регистрацию сразу при своём
        # подключении, и если канал к облаку в тот момент ещё не поднят
        # (гонка старта), кадр уходит в никуда: облако не видит серийник и
        # рвёт «молчаливую» сессию через таймаут. Реплей делается до
        # обычного форвардинга, поэтому облако получает серийник первым.
        #
        # Лок удерживается от рукопожатия до конца реплея: иначе кадр
        # модуля, пришедший в промежутке, ушёл бы в облако раньше
        # регистрации. Писать в недополученное TLS-соединение бессмысленно
        # — модуль никуда не денется и пришлёт следующий кадр.
        with self._write_lock:
            try:
                self._do_handshake(conn)
            except (SSL.Error, OSError) as err:
                self._log.warning("TLS-рукопожатие с облаком не удалось: %s", err)
                raise

            self._replay_registration_frame(conn)
            self._connected.set()
        self._log.warning("Подключение к облаку ZOTA %s:%d установлено", self._host, self._port)

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
                    self._log.warning("Облако закрыло TLS-соединение (zero return)")
                    break
                except (SSL.Error, OSError) as err:
                    if is_idle_error(err):
                        # EAGAIN на неблокирующем сокете — «данных нет»,
                        # а не обрыв соединения.
                        self._idle_wait()
                        continue
                    if not self._stop_event.is_set():
                        self._log.warning("Соединение с облаком закрыто: %s", err)
                    break
                if not data:
                    self._log.warning("Облако разорвало соединение (EOF)")
                    break
                self._handle_cloud_bytes(data)
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

    def _handle_cloud_bytes(self, data: bytes) -> None:
        """Обработать байты от облака: переслать модулю и разобрать локально."""
        log_frame("C2M", data)
        # Сначала прозрачный пересыл модулю — дословно, вместе с хвостами
        # keepalive-кадров (op=4, +10 байт) и любыми «сырыми» блобами.
        if self._send_to_module is not None:
            if not self._send_to_module(data):
                self._log.warning("Пересылка в модуль не удалась, сессия прерывается")
                raise OSError("не удалось переслать байты в модуль")
        self._feed_frames(self._cloud_frames, data, "CLOUD")

    def _replay_registration_frame(self, conn: SSL.Connection) -> None:
        """Отправить кэшированный кадр регистрации первым в новой сессии.

        Кэш живёт в координаторе: кадр регистрации перехватывается в пути
        модуль → облако и сохраняется целиком. Если кэша нет (модуль ещё
        не присылал регистрацию) — ничего не отправляется, первый кадр
        придёт обычным форвардингом.

        Повторные реконнекты облака реплеят тот же кадр: облако переваривает
        повторную регистрацию (эталонный шлюз работал так же неделями).
        """
        getter = self._get_registration_frame
        if getter is None:
            return
        try:
            frame = getter()
        except Exception:  # noqa: BLE001 — кэш не должен ронять сессию
            self._log.exception("Не удалось получить кэш кадра регистрации")
            return
        if not frame:
            return
        if not self._write(conn, frame):
            self._log.warning("Кадр регистрации не отправлен в облако")
            return
        self._log.info(
            "Кадр регистрации отправлен в облако первым (%d байт)", len(frame)
        )

    def _handle_registration_frame(self, frame) -> None:
        """Отдать координатору сырой кадр регистрации модуля (link=2 op=2).

        Кадр перехватывается в пути модуль → облако: именно здесь известны
        границы кадра, и именно отсюда он обычно уходит в облако «в никуда»
        (канала ещё нет). Координатор кэширует его и отдаёт мосту для
        реплея при следующем подключении к облаку.
        """
        if self._on_registration_frame is None:
            return
        raw = build_frame(frame.link, frame.opcode, frame.payload)
        try:
            self._on_registration_frame(raw)
        except Exception:  # noqa: BLE001 — сбой кэша не должен ронять мост
            self._log.exception("Колбэк on_registration_frame упал")

    def _feed_frames(self, buffer: FrameBuffer, data: bytes, source: str) -> None:
        """Разобрать пришедшие байты на кадры и вызвать ``on_frame``."""
        try:
            frames = buffer.feed(data)
        except ValueError as err:
            self._log.warning("Некорректный кадр (%s): %s", source, err)
            buffer.reset()
            return
        for frame in frames:
            self._log.debug(
                "%s RX link=%d opcode=%d len=%d",
                source,
                frame.link,
                frame.opcode,
                len(frame.payload),
            )
            # Регистрация модуля перехватывается только в направлении
            # модуль → облако: кадр от облака регистрацией не является.
            if (
                source == "MODULE"
                and frame.link == LINK_REGISTER
                and frame.opcode == OPCODE_REGISTER
            ):
                self._handle_registration_frame(frame)
            if self._on_frame is None:
                continue
            try:
                self._on_frame(frame.link, frame.opcode, frame.payload)
            except Exception:  # noqa: BLE001
                self._log.exception("Колбэк on_frame упал")

    def _write(self, conn: Optional[SSL.Connection], data: bytes) -> bool:
        """Записать байты в TLS-соединение с retry на SSLWantWrite."""
        if conn is None:
            return False
        offset = 0
        idle_rounds = 0
        while offset < len(data):
            try:
                written = conn.send(data[offset:])
            except SSL.WantWriteError:
                idle_rounds += 1
                if idle_rounds > WRITE_IDLE_LIMIT:
                    self._log.warning("TLS-запись в облако: сокет не готов писать")
                    return False
                self._idle_wait()
                continue
            except SSL.WantReadError:
                idle_rounds += 1
                if idle_rounds > WRITE_IDLE_LIMIT:
                    self._log.warning("TLS-запись в облако: сокет не готов читать")
                    return False
                self._idle_wait()
                continue
            except (SSL.Error, OSError) as err:
                if is_idle_error(err):
                    # EAGAIN при записи — «данных нет», а не обрыв.
                    idle_rounds += 1
                    if idle_rounds > WRITE_IDLE_LIMIT:
                        self._log.warning("TLS-запись в облако: сокет не готов писать")
                        return False
                    self._idle_wait()
                    continue
                self._log.warning("TLS-запись в облако не удалась: %s", err)
                return False
            idle_rounds = 0
            if written <= 0:
                return False
            offset += written
        return True

    def _close_connection(self) -> None:
        """Закрыть активное TLS-соединение с облаком и сокет."""
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
    "CONNECT_TIMEOUT",
    "LINK_REGISTER",
    "OPCODE_REGISTER",
    "RECONNECT_DELAY",
    "ZotaCloudBridge",
    "_install_psk_client_callback",
    "_load_openssl_binding",
    "parse_cloud_address",
]
