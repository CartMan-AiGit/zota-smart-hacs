"""Чистая логика кадров протокола ZOTA PO Smart v2.

Только стандартная библиотека (struct), никакого ввода-вывода: модуль не
открывает сокеты и не знает про TLS. Транспорт живёт отдельно.

Формат кадра (§11.1, §11.7):

    <u16 LE link, u16 LE opcode, u16 LE len> + payload

Примеры из живых логов:

    запись уставки 57:  01 00 00 00 04 00 02 00 39 00
    ACK:                01 00 01 00 04 00 02 00 00 00

Здесь же — общая для обоих транспортов (TLS-сервер модуля и мост к облаку)
проверка «данных нет»: неблокирующий сокет сообщает об отсутствии данных
исключением, а не пустым чтением, и такое состояние — не обрыв сессии.
"""

from __future__ import annotations

import errno
import os
import struct
from dataclasses import dataclass

# Модуль намеренно самодостаточен: только стандартная библиотека, без
# импорта .const и без homeassistant. Поэтому его можно использовать и
# тестировать в одиночку, вне пакета интеграции.
# Значения продублированы из const.py — при изменении править оба места.

_HEADER = struct.Struct("<HHH")
_U16 = struct.Struct("<H")
_U16_U16 = struct.Struct("<HH")

#: link=1 — плоский командный кадр.
LINK_FLAT = 1
#: opcode=0 — команда/запись.
OPCODE_COMMAND = 0
#: opcode=1 — ответ модуля (ACK).
OPCODE_ACK = 1
#: Размер заголовка кадра: link, opcode, len (3 × u16 LE).
HEADER_SIZE = 6

#: Максимальная длина payload в одном кадре. Значение — защитный предел от
#: бесконечного буфера при разборе мусора из сокета; в живом протоколе
#: максимум 57 байт (info-блок).
MAX_PAYLOAD = 1024


@dataclass(frozen=True)
class Frame:
    """Разобранный кадр."""

    link: int
    opcode: int
    payload: bytes

    @property
    def length(self) -> int:
        """Длина payload."""
        return len(self.payload)


def build_frame(link: int, opcode: int, payload: bytes = b"") -> bytes:
    """Собрать кадр: заголовок <link, opcode, len> + payload."""
    payload = bytes(payload)
    return _HEADER.pack(link, opcode, len(payload)) + payload


def build_read(cmd: int, link: int = LINK_FLAT) -> bytes:
    """Собрать кадр чтения: запрос cmd (payload = <u16 cmd>).

    Форма запрошенного кадра — `link=1 opcode=0` с payload `<u16 cmd>`
    (§11.4: cmd=0 → info, cmd=16 → паспорт).
    """
    return build_frame(link, OPCODE_COMMAND, _U16.pack(cmd))


def build_write(cmd: int, value: int, link: int = LINK_FLAT) -> bytes:
    """Собрать кадр записи: `link=1 opcode=0`, payload = <u16 cmd, u16 value>."""
    return build_frame(link, OPCODE_COMMAND, _U16_U16.pack(cmd, value))


def build_ack(cmd_echo: int, result: int, link: int = LINK_FLAT) -> bytes:
    """Собрать кадр ACK: `link=1 opcode=1`, payload = <u16 cmd_echo, u16 result>.

    Нужен для проверки разбора и для эмуляции модуля на стороне HA.
    """
    return build_frame(link, OPCODE_ACK, _U16_U16.pack(cmd_echo, result))


def parse_ack(payload: bytes) -> tuple[int, int]:
    """Разобрать ACK-payload: <u16 cmd_echo, u16 result> → (cmd_echo, result).

    result: 0 = OK с данными, 1 = OK пусто, 2 = NAK (§11.3).
    """
    if len(payload) < _U16_U16.size:
        raise ValueError(f"ACK payload too short: {len(payload)} bytes")
    return _U16_U16.unpack_from(payload, 0)


class FrameBuffer:
    """Накопитель входящих байт с выдачей целых кадров.

    Нужен потому, что TCP не сохраняет границы сообщений: один recv может
    принести половину кадра, два кадра или кадр с хвостом следующего.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """Добавить байты в буфер и вернуть список полных кадров."""
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buf) < HEADER_SIZE:
                break
            link, opcode, length = _HEADER.unpack_from(self._buf, 0)
            if length > MAX_PAYLOAD:
                raise ValueError(f"frame length out of range: {length}")
            total = HEADER_SIZE + length
            if len(self._buf) < total:
                break
            payload = bytes(self._buf[HEADER_SIZE:total])
            del self._buf[:total]
            frames.append(Frame(link=link, opcode=opcode, payload=payload))
        return frames

    @property
    def pending(self) -> int:
        """Сколько байт неполного кадра осталось в буфере."""
        return len(self._buf)

    def reset(self) -> None:
        """Сбросить буфер (например, после обрыва соединения)."""
        self._buf.clear()


#: Путь файла пассивного захвата кадров (в HA-контейнере /config — корень конфига).
FRAME_LOG_PATH = "/config/zota_frames.log"

#: Порог ротации файла захвата: при превышении текущий файл переименовывается
#: в ``.old`` (перезапись), и запись начинается заново.
FRAME_LOG_MAX_BYTES = 20 * 1024 * 1024

#: Включён ли захват кадров (опция интеграции ``frame_log``, по умолчанию выкл).
_FRAME_LOG_ENABLED = False


def set_frame_logging(enabled: bool) -> None:
    """Включить/выключить запись кадров в файл захвата.

    Флаг глобальный: транспорт и мост используют один и тот же модуль
    ``protocol``, а котёл в конфигурации HA один.
    """
    global _FRAME_LOG_ENABLED
    _FRAME_LOG_ENABLED = bool(enabled)


def _rotate_frame_log_if_needed() -> None:
    """Переименовать разросшийся лог захвата в ``.old`` (перезапись)."""
    try:
        if os.path.exists(FRAME_LOG_PATH) and (
            os.path.getsize(FRAME_LOG_PATH) > FRAME_LOG_MAX_BYTES
        ):
            os.replace(FRAME_LOG_PATH, FRAME_LOG_PATH + ".old")
    except OSError:
        pass


def log_frame(tag: str, data: bytes) -> None:
    """Пассивно дописать кадр в файл захвата. Никогда не бросает исключений.

    Пишет только когда захват включён (:func:`set_frame_logging`) — вызовы
    идут из потоков транспорта и не должны что-либо ломать при выключенной
    опции.
    """
    if not _FRAME_LOG_ENABLED:
        return
    try:
        from datetime import datetime

        _rotate_frame_log_if_needed()
        with open(FRAME_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(
                "%s %s len=%d %s\n"
                % (datetime.now().isoformat(timespec="milliseconds"), tag, len(data), data.hex())
            )
    except Exception:  # noqa: BLE001 — логирование не должно ронять управление
        pass


def parse_frame(buf: bytes) -> list[Frame]:
    """Разобрать буфер на кадры. Удобная обёртка над FrameBuffer.

    Возвращает только целые кадры; неполный хвост отбрасывается.
    """
    return FrameBuffer().feed(buf)


# --- Ошибки чтения сокета ---------------------------------------------------
#: Errno, означающие «данных пока нет» (сокет в неблокирующем режиме).
IDLE_ERRNOS = frozenset(
    value
    for value in (getattr(errno, "EAGAIN", None), getattr(errno, "EWOULDBLOCK", None))
    if value is not None
)

#: Текстовые имена тех же errno — по ним опознаётся EAGAIN в сообщении
#: исключения (pyOpenSSL кладёт «EAGAIN» то в errno, то только в текст).
IDLE_ERRNO_NAMES = frozenset({"EAGAIN", "EWOULDBLOCK"})

#: Пауза в цикле чтения при отсутствии данных, с.
IDLE_SLEEP = 0.1


def is_idle_error(err: BaseException) -> bool:
    """Похоже ли исключение на «данных нет» (EAGAIN/EWOULDBLOCK).

    Неблокирующий сокет сообщает об отсутствии данных исключением, а не
    пустым чтением: это либо ``OSError`` с ``errno=EAGAIN``, либо
    ``SSL.SysCallError(EAGAIN, ...)`` из pyOpenSSL (errno лежит в
    ``args[0]``, а текст — «EAGAIN»). Такое состояние — не обрыв
    соединения: цикл чтения обязан его пережить (пауза и продолжение),
    иначе живая сессия рвётся через пару секунд после установления.

    Настоящий обрыв — zero-return (чистый EOF) или прочие ошибки
    (``ECONNRESET`` и т. п.).
    """
    code = getattr(err, "errno", None)
    if code is None:
        args = getattr(err, "args", ())
        if args and isinstance(args[0], int):
            code = args[0]
    if code in IDLE_ERRNOS:
        return True
    # errno не распознан (или его нет вовсе) — ищем по тексту: pyOpenSSL
    # в разных версиях кладёт «EAGAIN» то в errno, то только в сообщение.
    for arg in getattr(err, "args", ()):
        if isinstance(arg, str) and arg.upper() in IDLE_ERRNO_NAMES:
            return True
    return False


__all__ = [
    "Frame",
    "FrameBuffer",
    "HEADER_SIZE",
    "IDLE_ERRNOS",
    "IDLE_ERRNO_NAMES",
    "IDLE_SLEEP",
    "LINK_FLAT",
    "MAX_PAYLOAD",
    "OPCODE_ACK",
    "OPCODE_COMMAND",
    "build_frame",
    "build_ack",
    "build_read",
    "build_write",
    "is_idle_error",
    "log_frame",
    "set_frame_logging",
    "parse_ack",
    "parse_frame",
]
