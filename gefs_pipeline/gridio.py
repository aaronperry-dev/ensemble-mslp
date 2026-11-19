"""Compact gzip'd binary grids for the browser (".bin.gz").

Layout (little endian), then the whole file is gzip'd:

    "GMS1"  u32 header_length  header (UTF-8 JSON)  field  [owner]

* header  - {kind, fh, valid, nx, ny, lon0, lat0, dlon, dlat, members, source, lows?}
            lon0/lat0 = first column/row (the grid is ascending in both); `lows` = [[lon, lat, hPa, member], ...]
* field   - nx*ny Int16 tenths of hPa (-32768 = missing), row-major, south -> north.  Other quantities (10 m wind, gust,
            in mph) use the header's "scale" = stored units per unit (default 10 = tenths; wind uses 2 = half-mph steps).  Each value is predicted from
            its neighbours (left + up - up-left, like PNG/LOCO "gradient"), the residual is zig-zag coded and split
            into a low-byte plane followed by a high-byte plane.  Smooth pressure fields become long runs of
            zeros, which gzip squeezes to a few bits per cell.  The browser reverses it exactly (lossless).
* owner   - (min files) nx*ny Uint8: index of the member that supplies the minimum at each cell.

Probability files (kind "prob") carry K planes instead of one field: header {levels:[hPa,...], members:N, ...} and,
for every level, an Int16 plane of "number of members with MSLP below that level" coded exactly like `field` above.
"""
from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path

import numpy as np

MAGIC = b"GMS1"
MISSING = -32768


def _pack_ints(v: np.ndarray) -> bytes:
    """int (ny, nx) -> predicted (left + up - up-left), zig-zagged, byte-plane-split Int16 residuals."""
    v = v.astype(np.int32)
    left = np.zeros_like(v); left[:, 1:] = v[:, :-1]
    up = np.zeros_like(v); up[1:, :] = v[:-1, :]
    ul = np.zeros_like(v); ul[1:, 1:] = v[:-1, :-1]
    r = (v - (left + up - ul)).astype(np.int16).astype(np.int32)           # modular 16-bit residual
    z = (((r << 1) ^ (r >> 31)) & 0xFFFF).astype(np.uint16)
    return (z & 0xFF).astype(np.uint8).tobytes() + (z >> 8).astype(np.uint8).tobytes()


def pack_field(values: np.ndarray, scale: float = 10.0) -> bytes:
    """float (ny, nx) -> Int16 units of 1/scale (hPa tenths by default; -32768 = missing), then `_pack_ints`."""
    return _pack_ints(np.where(np.isfinite(values), np.clip(np.rint(values * scale), -32767, 32767), MISSING))


def _unpack_ints(buf: bytes, nx: int, ny: int, offset: int = 0) -> np.ndarray:
    """Inverse of `_pack_ints`.  The predictor makes the value a 2-D running sum of the residuals (mod 2^16)."""
    n = nx * ny
    lo = np.frombuffer(buf, np.uint8, n, offset=offset).astype(np.int64)
    hi = np.frombuffer(buf, np.uint8, n, offset=offset + n).astype(np.int64)
    z = lo | (hi << 8)
    r = ((z >> 1) ^ -(z & 1)).reshape(ny, nx)
    out = np.cumsum(np.cumsum(r, axis=0), axis=1)
    return (((out + 32768) & 0xFFFF) - 32768).astype(np.int32)


def unpack_field(buf: bytes, nx: int, ny: int) -> np.ndarray:
    """Used by the tests (the browser has its own copy). Returns tenths as int32 (-32768 = missing)."""
    return _unpack_ints(buf, nx, ny)


def encode(header: dict, field: np.ndarray, owner: np.ndarray | None = None, scale: float = 10.0) -> bytes:
    header = dict(header, scale=scale) if scale != 10.0 else header
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = MAGIC + struct.pack("<I", len(h)) + h + pack_field(field, scale)
    if owner is not None:
        body += np.clip(owner, 0, 255).astype(np.uint8).tobytes()
    return gzip.compress(body, compresslevel=9, mtime=0)


def encode_counts(header: dict, planes: np.ndarray) -> bytes:
    """planes: (K, ny, nx) member counts, one plane per entry of header["levels"]."""
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = MAGIC + struct.pack("<I", len(h)) + h + b"".join(_pack_ints(p) for p in planes)
    return gzip.compress(body, compresslevel=9, mtime=0)


def decode_counts(blob: bytes) -> tuple[dict, np.ndarray]:
    raw = gzip.decompress(blob)
    assert raw[:4] == MAGIC
    (hl,) = struct.unpack_from("<I", raw, 4)
    hdr = json.loads(raw[8:8 + hl])
    nx, ny, k = hdr["nx"], hdr["ny"], len(hdr["levels"])
    off = 8 + hl
    return hdr, np.stack([_unpack_ints(raw, nx, ny, off + i * 2 * nx * ny) for i in range(k)])


def decode(blob: bytes) -> tuple[dict, np.ndarray, np.ndarray | None]:
    raw = gzip.decompress(blob)
    assert raw[:4] == MAGIC
    (hl,) = struct.unpack_from("<I", raw, 4)
    hdr = json.loads(raw[8:8 + hl])
    nx, ny = hdr["nx"], hdr["ny"]
    off = 8 + hl
    tenths = _unpack_ints(raw, nx, ny, off)
    off += 2 * nx * ny
    field = np.where(tenths == MISSING, np.nan, tenths / float(hdr.get("scale", 10)))
    owner = np.frombuffer(raw, np.uint8, nx * ny, offset=off).reshape(ny, nx) if len(raw) > off else None
    return hdr, field, owner


def write_counts(path: Path, header: dict, planes: np.ndarray) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = encode_counts(header, planes)
    path.write_bytes(blob)
    return len(blob)


def write(path: Path, header: dict, field: np.ndarray, owner: np.ndarray | None = None, scale: float = 10.0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = encode(header, field, owner, scale)
    path.write_bytes(blob)
    return len(blob)


def gzip_json_tree(root: Path) -> None:
    """Replace every *.json under `root` by *.json.gz (the viewer fetches the .gz and inflates it in the browser)."""
    for p in list(root.rglob("*.json")):
        p.with_suffix(".json.gz").write_bytes(gzip.compress(p.read_bytes(), compresslevel=9, mtime=0))
        p.unlink()
