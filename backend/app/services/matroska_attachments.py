"""Лёгкий pure-Python разбор Matroska Attachments (FileName / FileMimeType / size).

Без mkvtoolnix и без libmatroska: только EBML walk секции Attachments.
Бинарные FileData не загружаются в память (seek по размеру элемента).
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

# Защита от OOM: FileName / FileMimeType не должны быть гигантскими.
_MAX_STRING_ELEMENT = 64 * 1024

# Matroska / EBML element IDs (encoded form, as stored on disk).
_ID_EBML = 0x1A45DFA3
_ID_SEGMENT = 0x18538067
_ID_SEEK_HEAD = 0x114D9B74
_ID_SEEK = 0x4DBB
_ID_SEEK_ID = 0x53AB
_ID_SEEK_POSITION = 0x53AC
_ID_ATTACHMENTS = 0x1941A469
_ID_ATTACHED_FILE = 0x61A7
_ID_FILE_NAME = 0x466E
_ID_FILE_MIME_TYPE = 0x4660
_ID_FILE_DATA = 0x465C
_ID_CLUSTER = 0x1F43B675


class _EbmlError(Exception):
    pass


def _read_exact(fh: BinaryIO, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise _EbmlError("unexpected EOF")
    return data


def _read_element_id(fh: BinaryIO) -> int | None:
    first_b = fh.read(1)
    if not first_b:
        return None
    first = first_b[0]
    mask = 0x80
    length = 1
    while length <= 4 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 4 or mask == 0:
        raise _EbmlError(f"invalid EBML ID octet 0x{first:02x}")
    raw = bytes([first]) + (_read_exact(fh, length - 1) if length > 1 else b"")
    return int.from_bytes(raw, "big")


def _read_vint_size(fh: BinaryIO) -> int | None:
    """Размер элемента; ``None`` = unknown-size (все биты данных = 1)."""
    first_b = fh.read(1)
    if not first_b:
        raise _EbmlError("unexpected EOF in size")
    first = first_b[0]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or mask == 0:
        raise _EbmlError(f"invalid EBML size octet 0x{first:02x}")
    value = first & (mask - 1)
    if length > 1:
        rest = _read_exact(fh, length - 1)
        value = (value << (8 * (length - 1))) | int.from_bytes(rest, "big")
    all_ones = (1 << (7 * length)) - 1
    if value == all_ones:
        return None
    return value


def _read_element_head(fh: BinaryIO) -> tuple[int, int | None] | None:
    eid = _read_element_id(fh)
    if eid is None:
        return None
    return eid, _read_vint_size(fh)


def _decode_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").rstrip("\x00").strip()


def _parse_children(fh: BinaryIO, end: int) -> list[tuple[int, int, int]]:
    """Список (eid, data_start, size) до позиции end (исключая unknown-size)."""
    items: list[tuple[int, int, int]] = []
    while fh.tell() < end:
        head = _read_element_head(fh)
        if head is None:
            break
        eid, size = head
        if size is None:
            raise _EbmlError("unexpected unknown-size child")
        data_start = fh.tell()
        items.append((eid, data_start, size))
        fh.seek(data_start + size)
    return items


def _parse_seek_head(fh: BinaryIO, size: int) -> dict[int, int]:
    """SeekID -> SeekPosition (относительно начала данных Segment)."""
    end = fh.tell() + size
    out: dict[int, int] = {}
    for eid, data_start, child_size in _parse_children(fh, end):
        if eid != _ID_SEEK:
            continue
        fh.seek(data_start)
        seek_id: int | None = None
        seek_pos: int | None = None
        for cid, cstart, csize in _parse_children(fh, data_start + child_size):
            fh.seek(cstart)
            raw = _read_exact(fh, csize)
            if cid == _ID_SEEK_ID:
                seek_id = int.from_bytes(raw, "big")
            elif cid == _ID_SEEK_POSITION:
                seek_pos = int.from_bytes(raw, "big")
        if seek_id is not None and seek_pos is not None:
            out[seek_id] = seek_pos
    return out


def _read_string_element(fh: BinaryIO, data_start: int, size: int) -> str | None:
    """Читает UTF-8 строковый элемент; ``None`` если заявленный size слишком велик."""
    if size > _MAX_STRING_ELEMENT:
        return None
    fh.seek(data_start)
    return _decode_utf8(_read_exact(fh, size))


def _parse_attached_file(fh: BinaryIO, size: int) -> dict[str, object] | None:
    end = fh.tell() + size
    name = ""
    mime = ""
    size_bytes: int | None = None
    for eid, data_start, child_size in _parse_children(fh, end):
        if eid == _ID_FILE_NAME:
            decoded = _read_string_element(fh, data_start, child_size)
            if decoded is None:
                return None
            name = decoded
        elif eid == _ID_FILE_MIME_TYPE:
            decoded = _read_string_element(fh, data_start, child_size)
            if decoded is None:
                return None
            mime = decoded
        elif eid == _ID_FILE_DATA:
            # Размер вложения = размер элемента FileData; payload не читаем.
            size_bytes = child_size
    if not name:
        return None
    return {"name": name, "mime": mime, "size_bytes": size_bytes}


def _parse_attachments(fh: BinaryIO, size: int) -> list[dict[str, object]]:
    end = fh.tell() + size
    out: list[dict[str, object]] = []
    for eid, data_start, child_size in _parse_children(fh, end):
        if eid != _ID_ATTACHED_FILE:
            continue
        fh.seek(data_start)
        item = _parse_attached_file(fh, child_size)
        if item is not None:
            out.append(item)
    return out


def read_matroska_attachments(path: Path | str) -> list[dict[str, object]]:
    """Читает AttachedFile: name, mime (FileMimeType as stored), size_bytes.

    При ошибке / не-MKV возвращает ``[]``. MIME по расширению не выводит.
    """
    p = Path(path)
    try:
        with p.open("rb") as fh:
            return _read_attachments_from_stream(fh)
    except OSError as exc:
        logger.debug("matroska attachments: cannot open %s: %s", p, exc)
        return []
    except _EbmlError as exc:
        logger.debug("matroska attachments: parse error in %s: %s", p, exc)
        return []
    except Exception as exc:
        logger.warning("matroska attachments: unexpected error for %s: %s", p, exc)
        return []


def _read_attachments_from_stream(fh: BinaryIO) -> list[dict[str, object]]:
    segment_data_start: int | None = None
    segment_end: int | None = None

    while True:
        head = _read_element_head(fh)
        if head is None:
            return []
        eid, size = head
        if eid == _ID_EBML:
            if size is None:
                raise _EbmlError("EBML unknown size")
            fh.seek(size, 1)
            continue
        if eid == _ID_SEGMENT:
            segment_data_start = fh.tell()
            segment_end = None if size is None else segment_data_start + size
            break
        if size is None:
            raise _EbmlError("unknown-size non-Segment at top level")
        fh.seek(size, 1)

    assert segment_data_start is not None

    # Prefer SeekHead → Attachments jump; иначе линейный scan Segment
    # (Cluster payload пропускаем seek'ом по size — без чтения гигабайтов).
    attachments_rel: int | None = None
    scan_pos = segment_data_start

    while True:
        if segment_end is not None and scan_pos >= segment_end:
            break
        fh.seek(scan_pos)
        head = _read_element_head(fh)
        if head is None:
            break
        eid, size = head
        data_start = fh.tell()
        if size is None:
            # unknown-size level-1 (обычно Cluster) — без size нельзя безопасно
            # перепрыгнуть payload; Attachments после такого Cluster не найдём.
            break
        next_pos = data_start + size

        if eid == _ID_SEEK_HEAD:
            seeks = _parse_seek_head(fh, size)
            if _ID_ATTACHMENTS in seeks:
                attachments_rel = seeks[_ID_ATTACHMENTS]
                break
            # SeekHead без указателя на Attachments — продолжаем scan.
        elif eid == _ID_ATTACHMENTS:
            return _parse_attachments(fh, size)

        # Cluster и прочие level-1: просто seek к next_pos (не читаем payload).
        scan_pos = next_pos

    if attachments_rel is None:
        return []

    fh.seek(segment_data_start + attachments_rel)
    head = _read_element_head(fh)
    if head is None:
        return []
    eid, size = head
    if eid != _ID_ATTACHMENTS or size is None:
        return []
    return _parse_attachments(fh, size)


def _encode_id(element_id: int) -> bytes:
    bit_len = max(element_id.bit_length(), 1)
    length = (bit_len + 7) // 8
    return element_id.to_bytes(length, "big")


def _encode_size(size: int) -> bytes:
    for length in range(1, 9):
        max_val = (1 << (7 * length)) - 1
        if size >= max_val:
            continue
        out = bytearray(length)
        v = size
        for i in range(length - 1, -1, -1):
            out[i] = v & 0xFF
            v >>= 8
        out[0] |= 1 << (8 - length)
        return bytes(out)
    raise ValueError("size too large")


def _elem(eid: int, payload: bytes) -> bytes:
    return _encode_id(eid) + _encode_size(len(payload)) + payload


def _attachments_payload(attachments: list[tuple[str, str, bytes]]) -> bytes:
    attached_blobs: list[bytes] = []
    for file_name, mime_type, file_data in attachments:
        attached_blobs.append(
            _elem(
                _ID_ATTACHED_FILE,
                b"".join(
                    [
                        _elem(_ID_FILE_NAME, file_name.encode("utf-8")),
                        _elem(_ID_FILE_MIME_TYPE, mime_type.encode("utf-8")),
                        _elem(_ID_FILE_DATA, file_data),
                    ]
                ),
            )
        )
    return b"".join(attached_blobs)


def build_minimal_mkv_with_attachments(
    attachments: list[tuple[str, str, bytes]],
) -> bytes:
    """Минимальный MKV (EBML+Segment+Attachments) для юнит-тестов.

    Каждый элемент ``attachments``: (file_name, mime_type, file_data).
    """
    ebml_master = _elem(_ID_EBML, _elem(0x4282, b"matroska"))  # DocType
    segment = _elem(_ID_SEGMENT, _elem(_ID_ATTACHMENTS, _attachments_payload(attachments)))
    return ebml_master + segment


def build_mkv_attachments_after_cluster(
    attachments: list[tuple[str, str, bytes]],
    *,
    cluster_payload: bytes = b"\x00" * 64,
) -> bytes:
    """MKV без SeekHead: Cluster, затем Attachments (для теста линейного scan)."""
    ebml_master = _elem(_ID_EBML, _elem(0x4282, b"matroska"))
    segment_body = b"".join(
        [
            _elem(_ID_CLUSTER, cluster_payload),
            _elem(_ID_ATTACHMENTS, _attachments_payload(attachments)),
        ]
    )
    return ebml_master + _elem(_ID_SEGMENT, segment_body)


def read_matroska_attachments_bytes(data: bytes) -> list[dict[str, object]]:
    """То же, что ``read_matroska_attachments``, но из буфера (тесты)."""
    try:
        return _read_attachments_from_stream(BytesIO(data))
    except _EbmlError:
        return []
