"""Convert sky brightness to the Bortle scale and naked-eye limiting magnitude."""

from __future__ import annotations

import math

# Below this the meter is reading twilight, daylight, or a covered dome, so
# a sky class would be misleading.
NIGHT_MINIMUM_MPSAS = 16.0

# Lower mag/arcsec² bound of each class, darkest first (commonly published
# SQM-to-Bortle bands).
BORTLE_BANDS = (
    (21.99, 1, "Excellent dark-sky site"),
    (21.89, 2, "Typical truly dark site"),
    (21.69, 3, "Rural sky"),
    (20.49, 4, "Rural/suburban transition"),
    (19.50, 5, "Suburban sky"),
    (18.94, 6, "Bright suburban sky"),
    (18.38, 7, "Suburban/urban transition"),
    (18.00, 8, "City sky"),
    (float("-inf"), 9, "Inner-city sky"),
)


def _band(mpsas: float) -> tuple[float, int, str] | None:
    if mpsas is None or not math.isfinite(mpsas) or mpsas < NIGHT_MINIMUM_MPSAS:
        return None
    return next(band for band in BORTLE_BANDS if mpsas >= band[0])


def bortle(mpsas: float) -> int | None:
    band = _band(mpsas)
    return band[1] if band else None


def nelm(mpsas: float) -> float | None:
    """Naked-eye limiting magnitude: 7.93 − 5·log10(10^(4.316 − m/5) + 1)."""
    if _band(mpsas) is None:
        return None
    return round(7.93 - 5 * math.log10(10 ** (4.316 - mpsas / 5) + 1), 2)


def describe(mpsas: float) -> dict:
    band = _band(mpsas)
    return {
        "bortle": band[1] if band else None,
        "bortle_name": band[2] if band else None,
        "nelm": nelm(mpsas),
    }
