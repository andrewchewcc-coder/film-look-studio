"""DNG lens corrections that LibRaw leaves out.

Leica (and many phone/compact) DNGs store distortion and vignetting corrections as
DNG "opcodes" plus a default crop; without them the image is barrel-distorted with
dark, curved corners. This module reads those tags with a minimal TIFF parser and
applies WarpRectilinear, FixVignetteRadial and DefaultCrop.
"""
import math
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

TAG_SUBIFDS, TAG_NEWSUBFILETYPE, TAG_PHOTOMETRIC = 0x014A, 0x00FE, 0x0106
TAG_CROP_ORIGIN, TAG_CROP_SIZE, TAG_OPCODES3 = 0xC61F, 0xC620, 0xC74E
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
CFA = 32803


@dataclass
class DngInfo:
    crop_origin: tuple = None   # (x, y) in active-area pixels
    crop_size: tuple = None     # (w, h)
    warp: list = field(default_factory=list)       # [(coeffs[6] per plane..., cx, cy)]
    vignette: list = field(default_factory=list)   # [(k0..k4, cx, cy)]
    unsupported: list = field(default_factory=list)


def _read_ifd(f, offset, endian):
    f.seek(offset)
    (n,) = struct.unpack(endian + "H", f.read(2))
    tags = {}
    for _ in range(n):
        tag, typ, count, raw = struct.unpack(endian + "HHI4s", f.read(12))
        size = TYPE_SIZES.get(typ, 1) * count
        if size > 4:
            (ptr,) = struct.unpack(endian + "I", raw)
            here = f.tell()
            f.seek(ptr)
            data = f.read(size)
            f.seek(here)
        else:
            data = raw[:size]
        tags[tag] = (typ, count, data)
    return tags


def _values(endian, typ, count, data):
    if typ in (7, 1, 6, 2):
        return data
    fmt = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d", 13: "I"}.get(typ)
    if fmt:
        return list(struct.unpack(f"{endian}{count}{fmt}", data))
    if typ in (5, 10):
        nums = struct.unpack(f"{endian}{count * 2}{'I' if typ == 5 else 'i'}", data)
        return [nums[i] / nums[i + 1] if nums[i + 1] else 0.0 for i in range(0, len(nums), 2)]
    return data


def _parse_opcodes(blob, info):
    (count,) = struct.unpack(">I", blob[:4])
    pos = 4
    for _ in range(count):
        op_id, _ver, flags, size = struct.unpack(">IIII", blob[pos:pos + 16])
        params = blob[pos + 16:pos + 16 + size]
        pos += 16 + size
        if op_id == 1:  # WarpRectilinear
            (planes,) = struct.unpack(">I", params[:4])
            nums = struct.unpack(f">{planes * 6 + 2}d", params[4:4 + (planes * 6 + 2) * 8])
            coeffs = [nums[i * 6:(i + 1) * 6] for i in range(planes)]
            info.warp.append((coeffs, nums[-2], nums[-1]))
        elif op_id == 3:  # FixVignetteRadial
            info.vignette.append(struct.unpack(">7d", params[:56]))
        elif not (flags & 1):  # optional opcodes may be skipped silently
            info.unsupported.append(op_id)


def read_info(path):
    """Read crop and lens-correction opcodes from the main (full-resolution raw) IFD."""
    info = DngInfo()
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:2] not in (b"II", b"MM"):
            return info
        endian = "<" if head[:2] == b"II" else ">"
        (first,) = struct.unpack(endian + "I", head[4:8])
        ifds, queue, seen = [], [first], set()
        while queue:
            off = queue.pop(0)
            if not off or off in seen:
                continue
            seen.add(off)
            tags = _read_ifd(f, off, endian)
            ifds.append(tags)
            if TAG_SUBIFDS in tags:
                queue.extend(_values(endian, *tags[TAG_SUBIFDS]))
        main = None
        for tags in ifds:
            sub = _values(endian, *tags[TAG_NEWSUBFILETYPE])[0] if TAG_NEWSUBFILETYPE in tags else 0
            photometric = _values(endian, *tags[TAG_PHOTOMETRIC])[0] if TAG_PHOTOMETRIC in tags else None
            if sub == 0 and photometric in (CFA, 34892):  # CFA or LinearRaw
                main = tags
                break
        if main is None:
            return info
        if TAG_CROP_ORIGIN in main and TAG_CROP_SIZE in main:
            info.crop_origin = tuple(_values(endian, *main[TAG_CROP_ORIGIN]))
            info.crop_size = tuple(_values(endian, *main[TAG_CROP_SIZE]))
        if TAG_OPCODES3 in main:
            _parse_opcodes(main[TAG_OPCODES3][2], info)
    return info


def _bilinear(src, xs, ys):
    """Sample a single-channel image at float coordinates."""
    h, w = src.shape
    xs = np.clip(xs, 0, w - 1.001)
    ys = np.clip(ys, 0, h - 1.001)
    x0 = xs.astype(np.int32)
    y0 = ys.astype(np.int32)
    fx = xs - x0
    fy = ys - y0
    top = src[y0, x0] * (1 - fx) + src[y0, x0 + 1] * fx
    bot = src[y0 + 1, x0] * (1 - fx) + src[y0 + 1, x0 + 1] * fx
    return top * (1 - fy) + bot * fy


def _radius_norm(w, h, cx, cy):
    return max(math.hypot(x - cx, y - cy) for x in (0, w - 1) for y in (0, h - 1))


def warp_rectilinear(img, warp, rows=256):
    coeffs, rcx, rcy = warp
    h, w = img.shape[:2]
    cx, cy = rcx * (w - 1), rcy * (h - 1)
    m = _radius_norm(w, h, cx, cy)
    out = np.empty_like(img)
    xs = ((np.arange(w, dtype=np.float32) - cx) / m)[None, :]
    planes = coeffs if len(coeffs) == img.shape[2] else [coeffs[0]] * img.shape[2]

    def strip(y0):
        y1 = min(h, y0 + rows)
        dy = ((np.arange(y0, y1, dtype=np.float32) - cy) / m)[:, None]
        r2 = xs * xs + dy * dy
        for ch, (kr0, kr1, kr2, kr3, kt0, kt1) in enumerate(planes):
            f = kr0 + r2 * (kr1 + r2 * (kr2 + r2 * kr3))
            sx = cx + m * (f * xs + kt0 * 2 * xs * dy + kt1 * (r2 + 2 * xs * xs))
            sy = cy + m * (f * dy + kt1 * 2 * xs * dy + kt0 * (r2 + 2 * dy * dy))
            vals = _bilinear(img[..., ch], sx, sy)
            if img.dtype.kind == "u":
                vals = np.clip(vals + 0.5, 0, np.iinfo(img.dtype).max)
            out[y0:y1, :, ch] = vals

    with ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1))) as pool:
        list(pool.map(strip, range(0, h, rows)))
    return out


def fix_vignette(img, vig, rows=256):
    k0, k1, k2, k3, k4, rcx, rcy = vig
    h, w = img.shape[:2]
    cx, cy = rcx * (w - 1), rcy * (h - 1)
    m = _radius_norm(w, h, cx, cy)
    xs = ((np.arange(w) - cx) / m) ** 2
    top = np.iinfo(img.dtype).max if img.dtype.kind == "u" else None
    for y0 in range(0, h, rows):
        y1 = min(h, y0 + rows)
        r2 = xs[None, :] + (((np.arange(y0, y1) - cy) / m) ** 2)[:, None]
        g = 1 + r2 * (k0 + r2 * (k1 + r2 * (k2 + r2 * (k3 + r2 * k4))))
        img[y0:y1] = np.clip(img[y0:y1] * g[..., None], 0, top).astype(img.dtype)
    return img


def apply(img, info, scale=1.0):
    """Apply opcodes then the default crop.

    `scale` is the size of `img` relative to the full raw active area (e.g. 0.5 for a
    half-size decode); opcode coordinates are relative, the crop is scaled.
    """
    for v in info.vignette:
        img = fix_vignette(img, v)
    for wp in info.warp:
        img = warp_rectilinear(img, wp)
    if info.crop_origin and info.crop_size:
        x, y = (int(round(v * scale)) for v in info.crop_origin)
        cw, ch = (int(round(v * scale)) for v in info.crop_size)
        img = img[y:y + ch, x:x + cw]
    return img
