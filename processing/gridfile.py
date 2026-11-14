"""Daily grid files: values, not colours, in one gzip file per UTC day that a browser can read.

Layout of the file after gzip decompression (all numbers little-endian):

    bytes 0-7     ASCII "SWFLGRD1"
    bytes 8-11    uint32 header length N
    bytes 12..    N bytes of UTF-8 JSON header, padded with spaces so the data starts on a
                  multiple of 8
    data          one block per layer, at header.layers[name].offset bytes from the start of the
                  data section, a C-order array of header.layers[name].shape
                  (hours x rows x cols, or rows x cols for static layers). Rows run north to south.

Layers are delta coded so gzip can shrink them (smooth fields that change slowly hour to hour):

    encoding "none"      stored as is
    encoding "delta_tx"  int16. Undo along hours, then along columns:
                         for t >= 1: a[t] += a[t-1];  then for x >= 1: a[.., x] += a[.., x-1]
    encoding "delta_t8"  uint8. Undo along hours with wrap-around: a[t] = (a[t] + a[t-1]) & 255

The decoded integer q gives value = q * scale + add_offset. Cells where the static layer named
by header.valid_mask equals its nodata value hold no data, and so does a whole hour of a layer
when header.hours[t].has[layer] is false. In a browser:

    const res = await fetch(url);
    const raw = await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
    const n = new DataView(raw).getUint32(8, true);
    const header = JSON.parse(new TextDecoder().decode(new Uint8Array(raw, 12, n)));
    const L = header.layers.temp_c, base = 12 + n;
    const [nt, ny, nx] = L.shape;
    const a = new Int16Array(raw.slice(base + L.offset, base + L.offset + nt * ny * nx * 2));
    const plane = ny * nx;
    for (let i = plane; i < a.length; i++) a[i] += a[i - plane];          // undo hours
    for (let r = 0; r < nt * ny; r++)                                      // undo columns
      for (let x = 1, o = r * nx; x < nx; x++) a[o + x] += a[o + x - 1];
    // temperature in degrees C at hour t, row y, col x: a[t*plane + y*nx + x] * L.scale

(If a server already decoded the gzip, the first two bytes are not 0x1f 0x8b; skip the
DecompressionStream step then.)
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np

MAGIC = b"SWFLGRD1"
FORMAT = "swfl-grid"
VERSION = 1


def encode(a: np.ndarray, encoding: str) -> np.ndarray:
    if encoding == "none":
        return a
    if encoding == "delta_tx":
        q = a.astype(np.int32)
        r = np.diff(q, axis=-1, prepend=0)
        r = np.diff(r, axis=0, prepend=0)
        if np.abs(r).max(initial=0) > 32767:
            raise ValueError("delta out of int16 range")
        return r.astype(np.int16)
    if encoding == "delta_t8":
        q = a.astype(np.int16)
        return (np.diff(q, axis=0, prepend=0) & 255).astype(np.uint8)
    raise ValueError(encoding)


def decode(a: np.ndarray, encoding: str) -> np.ndarray:
    if encoding == "none":
        return a
    if encoding == "delta_tx":
        r = np.cumsum(a.astype(np.int32), axis=0)
        return np.cumsum(r, axis=-1).astype(np.int16)
    if encoding == "delta_t8":
        return (np.cumsum(a.astype(np.int64), axis=0) & 255).astype(np.uint8)
    raise ValueError(encoding)


def write(path: Path, header: dict, layers: dict[str, tuple[np.ndarray, dict]]) -> int:
    """layers: name -> (decoded integer array, attributes incl. 'encoding'). Returns bytes written."""
    meta = {}
    blocks = []
    offset = 0
    for name, (arr, attrs) in layers.items():
        enc = attrs.get("encoding", "none")
        a = np.ascontiguousarray(encode(arr, enc))
        a = a.astype(a.dtype.newbyteorder("<"))
        meta[name] = dict(attrs, encoding=enc, dtype=a.dtype.name, shape=list(a.shape), offset=offset)
        b = a.tobytes()
        pad = (-len(b)) % 8
        blocks.append(b + b"\0" * pad)
        offset += len(b) + pad
    head = dict(header, format=FORMAT, version=VERSION, layers=meta)
    hb = json.dumps(head, separators=(",", ":")).encode()
    hb += b" " * ((-(12 + len(hb))) % 8)
    raw = MAGIC + np.uint32(len(hb)).astype("<u4").tobytes() + hb + b"".join(blocks)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(gzip.compress(raw, compresslevel=9, mtime=0))
    tmp.replace(path)
    return path.stat().st_size


def read(path: Path) -> tuple[dict, dict[str, np.ndarray]]:
    """Header and decoded integer arrays (before scale and masking)."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:8] != MAGIC:
        raise ValueError(f"{path} is not a {FORMAT} file")
    n = int(np.frombuffer(raw[8:12], "<u4")[0])
    head = json.loads(raw[12:12 + n].decode())
    base = 12 + n
    arrays = {}
    for name, m in head["layers"].items():
        count = int(np.prod(m["shape"]))
        dt = np.dtype(m["dtype"]).newbyteorder("<")
        a = np.frombuffer(raw, dt, count, base + m["offset"]).reshape(m["shape"])
        arrays[name] = decode(a, m.get("encoding", "none"))
    return head, arrays
