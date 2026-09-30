"""Minimal GGUF metadata reader — enough for `yantra models add` to inspect local GGUF files."""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any, BinaryIO

GGUF_MAGIC = b"GGUF"

_SCALARS: dict[int, tuple[str, int]] = {
    0: ("<B", 1),  # uint8
    1: ("<b", 1),  # int8
    2: ("<H", 2),  # uint16
    3: ("<h", 2),  # int16
    4: ("<I", 4),  # uint32
    5: ("<i", 4),  # int32
    6: ("<f", 4),  # float32
    7: ("<B", 1),  # bool
    10: ("<Q", 8),  # uint64
    11: ("<q", 8),  # int64
    12: ("<d", 8),  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9


class GGUFError(ValueError):
    pass


def _read(fh: BinaryIO, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise GGUFError("truncated GGUF file")
    return data


def _read_string(fh: BinaryIO) -> str:
    (length,) = struct.unpack("<Q", _read(fh, 8))
    if length > 1 << 24:
        raise GGUFError("implausible string length")
    return _read(fh, int(length)).decode("utf-8", errors="replace")


def _read_value(fh: BinaryIO, vtype: int, *, max_array: int = 4096) -> Any:
    if vtype in _SCALARS:
        fmt, size = _SCALARS[vtype]
        (value,) = struct.unpack(fmt, _read(fh, size))
        return bool(value) if vtype == 7 else value
    if vtype == _TYPE_STRING:
        return _read_string(fh)
    if vtype == _TYPE_ARRAY:
        (item_type,) = struct.unpack("<I", _read(fh, 4))
        (count,) = struct.unpack("<Q", _read(fh, 8))
        if count > max_array:
            # Skip huge arrays (tokenizer vocab) without materialising them.
            for _ in range(int(count)):
                _read_value(fh, item_type, max_array=0)
            return f"<array of {count}>"
        return [_read_value(fh, item_type, max_array=max_array) for _ in range(int(count))]
    raise GGUFError(f"unknown GGUF value type {vtype}")


def read_gguf_metadata(
    path: Path, wanted_prefixes: tuple[str, ...] = ("general.",)
) -> dict[str, Any]:
    """Header metadata kv pairs (filtered to wanted_prefixes; '' collects everything)."""
    with path.open("rb") as fh:
        if _read(fh, 4) != GGUF_MAGIC:
            raise GGUFError("not a GGUF file")
        (version,) = struct.unpack("<I", _read(fh, 4))
        if version < 2:
            raise GGUFError(f"unsupported GGUF version {version}")
        (tensor_count,) = struct.unpack("<Q", _read(fh, 8))
        (kv_count,) = struct.unpack("<Q", _read(fh, 8))
        if tensor_count > 1_000_000 or kv_count > 1_000_000:
            raise GGUFError("Implausible GGUF header counts")
        meta: dict[str, Any] = {"gguf.version": version}
        for _ in range(int(kv_count)):
            key = _read_string(fh)
            (vtype,) = struct.unpack("<I", _read(fh, 4))
            value = _read_value(fh, vtype)
            if not wanted_prefixes or any(key.startswith(p) for p in wanted_prefixes):
                meta[key] = value
        # Count stored tensor elements, rather than infer parameters from quantized bytes.
        parameters = 0
        for _ in range(tensor_count):
            _read_string(fh)
            (dimensions,) = struct.unpack("<I", _read(fh, 4))
            if not 1 <= dimensions <= 4:
                raise GGUFError("Invalid tensor dimensions")
            shape = struct.unpack("<" + "Q" * dimensions, _read(fh, 8 * dimensions))
            parameters += math.prod(shape)
            _read(fh, 12)  # ggml type and data offset
        if tensor_count:
            meta["general.parameter_count"] = parameters
        return meta
