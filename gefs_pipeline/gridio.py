"""Compact gzip'd binary grids for the browser (".bin.gz").

Layout (little endian), then the whole file is gzip'd:

    "GMS1"  u32 header_length  header (UTF-8 JSON)  field  [owner]

* header  - {kind, fh, valid, nx, ny, lon0, lat0, dlon, dlat, members, source, lows?}
            lon0/lat0 = first column/row (the grid is ascending in both); `lows` = [[lon, lat, hPa, member], ...]
* field   - nx*ny Int16 tenths of hPa (-32768 = missing), row-major, south -> north.  Each value is predicted from
            its neighbours (left + up - up-left, like PNG/LOCO "gradient"), the residual is zig-zag coded and split
            into a low-byte plane followed by a high-byte plane.  Smooth pressure fields become long runs of
            zeros, which gzip squeezes to a few bits per cell.  The browser reverses it exactly (lossless).
* owner   - (min files) nx*ny Uint8: index of the member that supplies the minimum at each cell.
"""
from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path

import numpy as np

MAGIC = b"GMS1"
MISSING = -32768


def pack_field(values: np.ndarray) -> bytes:
    """float hPa (ny, nx) -> predicted, zig-zagged, byte-plane-split Int16 residuals."""
    v = np.where(np.isfinite(values), np.rint(values * 10.0), MISSING).astype(np.int32)
    ny, nx = v.shape
    left = np.zeros_like(v); left[:, 1:] = v[:, :-1]
    up = np.zeros_like(v); up[1:, :] = v[:-1, :]
    ul = np.zeros_like(v); ul[1:, 1:] = v[:-1, :-1]
    r = (v - (left + up - ul)).astype(np.int16).astype(np.int32)           # modular 16-bit residual
    z = (((r << 1) ^ (r >> 31)) & 0xFFFF).astype(np.uint16)
    return (z & 0xFF).astype(np.uint8).tobytes() + (z >> 8).astype(np.uint8).tobytes()


def unpack_field(buf: bytes, nx: int, ny: int) -> np.ndarray:
    """Inverse of pack_field (used by the tests; the browser has its own copy). Returns tenths as int32 (-32768 = missing)."""
    n = nx * ny
    lo = np.frombuffer(buf, np.uint8, n).astype(np.int32)
    hi = np.frombuffer(buf, np.uint8, n, offset=n).astype(np.int32)
    z = lo | (hi << 8)
    r = ((z >> 1) ^ -(z & 1)).reshape(ny, nx)
    out = np.zeros((ny, nx), np.int32)
    wrap = lambda a: ((a + 32768) & 0xFFFF) - 32768
    for j in range(ny):
        for i in range(nx):
            pred = (out[j, i - 1] if i else 0) + (out[j - 1, i] if j else 0) - (out[j - 1, i - 1] if i and j else 0)
            out[j, i] = wrap(pred + r[j, i])
    return out


def encode(header: dict, field: np.ndarray, owner: np.ndarray | None = None) -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = MAGIC + struct.pack("<I", len(h)) + h + pack_field(field)
    if owner is not None:
        body += np.clip(owner, 0, 255).astype(np.uint8).tobytes()
    return gzip.compress(body, compresslevel=9, mtime=0)


def decode(blob: bytes) -> tuple[dict, np.ndarray, np.ndarray | None]:
    raw = gzip.decompress(blob)
    assert raw[:4] == MAGIC
    (hl,) = struct.unpack_from("<I", raw, 4)
    hdr = json.loads(raw[8:8 + hl])
    nx, ny = hdr["nx"], hdr["ny"]
    off = 8 + hl
    tenths = unpack_field(raw[off:off + 2 * nx * ny], nx, ny)
    off += 2 * nx * ny
    field = np.where(tenths == MISSING, np.nan, tenths / 10.0)
    owner = np.frombuffer(raw, np.uint8, nx * ny, offset=off).reshape(ny, nx) if len(raw) > off else None
    return hdr, field, owner


def write(path: Path, header: dict, field: np.ndarray, owner: np.ndarray | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = encode(header, field, owner)
    path.write_bytes(blob)
    return len(blob)


def gzip_json_tree(root: Path) -> None:
    """Replace every *.json under `root` by *.json.gz (the viewer fetches the .gz and inflates it in the browser)."""
    for p in list(root.rglob("*.json")):
        p.with_suffix(".json.gz").write_bytes(gzip.compress(p.read_bytes(), compresslevel=9, mtime=0))
        p.unlink()
