"""ColorBrewer ramps and perceptual (OKLab) interpolation to N classes.

ColorBrewer 2.0 colours (c) Cynthia Brewer, Mark Harrower and The Pennsylvania State University,
Apache-2.0 licensed.  Hex values are the official 9-class sequential / 11-class diverging sets.
"""
from __future__ import annotations

import bisect
import math

RAMPS: dict[str, list[str]] = {
    # sequential (9)
    "YlOrRd": ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c", "#fc4e2a", "#e31a1c", "#bd0026", "#800026"],
    "YlOrBr": ["#ffffe5", "#fff7bc", "#fee391", "#fec44f", "#fe9929", "#ec7014", "#cc4c02", "#993404", "#662506"],
    "YlGnBu": ["#ffffd9", "#edf8b1", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8", "#253494", "#081d58"],
    "PuBu": ["#fff7fb", "#ece7f2", "#d0d1e6", "#a6bddb", "#74a9cf", "#3690c0", "#0570b0", "#045a8d", "#023858"],
    "Blues": ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b"],
    # diverging (11)
    "RdBu": ["#67001f", "#b2182b", "#d6604d", "#f4a582", "#fddbc7", "#f7f7f7", "#d1e5f0", "#92c5de", "#4393c3", "#2166ac", "#053061"],
    "RdYlBu": ["#a50026", "#d73027", "#f46d43", "#fdae61", "#fee090", "#ffffbf", "#e0f3f8", "#abd9e9", "#74add1", "#4575b4", "#313695"],
    "PuOr": ["#7f3b08", "#b35806", "#e08214", "#fdb863", "#fee0b6", "#f7f7f7", "#d8daeb", "#b2abd2", "#8073ac", "#542788", "#2d004b"],
}


# ---- sRGB <-> OKLab (Bjorn Ottosson) -------------------------------------------------------------
def _to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_lin(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_oklab(h: str) -> tuple[float, float, float]:
    r, g, b = (_to_lin(int(h[i:i + 2], 16) / 255) for i in (1, 3, 5))
    l = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def oklab_to_hex(L: float, a: float, b: float) -> str:
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )
    return "#" + "".join(f"{round(_from_lin(c) * 255):02x}" for c in rgb)


# ---- public API --------------------------------------------------------------------------------
def sample_ramp(anchors: list[str], n: int, mid_index: int | None = None) -> list[str]:
    """Return n hex colours interpolated along `anchors` in OKLab.

    mid_index (diverging ramps): the class that must receive the neutral centre anchor.  Each side of
    the ramp is then stretched independently so the neutral colour sits exactly on that class even when
    there are more classes on one side than the other.
    """
    lab = [hex_to_oklab(h) for h in anchors]
    if n == 1:
        return [anchors[len(anchors) // 2]]
    ts = []
    for i in range(n):
        if mid_index is None:
            ts.append(i / (n - 1))
        elif i <= mid_index:
            ts.append(0.5 * i / mid_index if mid_index else 0.5)
        else:
            ts.append(0.5 + 0.5 * (i - mid_index) / (n - 1 - mid_index))
    out = []
    for t in ts:
        pos = t * (len(lab) - 1)
        i0 = min(int(math.floor(pos)), len(lab) - 2)
        f = pos - i0
        c = tuple(lab[i0][k] + (lab[i0 + 1][k] - lab[i0][k]) * f for k in range(3))
        out.append(oklab_to_hex(*c))
    return out


def edges_from_spec(spec) -> list[float]:
    """{start, stop, step} (inclusive) -> list of class edges."""
    n = int(round((spec.stop - spec.start) / spec.step))
    return [spec.start + i * spec.step for i in range(n + 1)]


def build_scale(product_cfg) -> tuple[list[float], list[str]]:
    """Class edges and one colour per class.  N edges -> N+1 classes (open-ended first and last)."""
    edges = edges_from_spec(product_cfg.edges)
    n_classes = len(edges) + 1
    anchors = list(RAMPS[product_cfg.palette])
    if getattr(product_cfg, "reverse", False):
        anchors = anchors[::-1]
    center = getattr(product_cfg, "diverging_center", None)
    mid = bisect.bisect_right(edges, center) if center is not None else None
    return edges, sample_ramp(anchors, n_classes, mid)
