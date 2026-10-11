"""Colour-space helpers: sRGB transfer functions and OKLab.

All functions take and return float32 arrays shaped (..., 3).
"""
import numpy as np

_M1 = np.array([
    [0.4122214708, 0.5363325363, 0.0514459929],
    [0.2119034982, 0.6806995451, 0.1073969566],
    [0.0883024619, 0.2817188376, 0.6299787005],
], dtype=np.float32)
_M2 = np.array([
    [0.2104542553, 0.7936177850, -0.0040720468],
    [1.9779984951, -2.4285922050, 0.4505937099],
    [0.0259040371, 0.7827717662, -0.8086757660],
], dtype=np.float32)
_M1_INV = np.linalg.inv(_M1).astype(np.float32)
_M2_INV = np.linalg.inv(_M2).astype(np.float32)

# Rec.709 / sRGB luminance weights (linear light).
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def srgb_decode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)


def srgb_encode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055).astype(np.float32)


def linear_to_oklab(rgb):
    lms = np.maximum(rgb @ _M1.T, 0.0)
    return (np.cbrt(lms) @ _M2.T).astype(np.float32)


def oklab_to_linear(lab):
    lms = (lab @ _M2_INV.T) ** 3
    return (lms @ _M1_INV.T).astype(np.float32)
