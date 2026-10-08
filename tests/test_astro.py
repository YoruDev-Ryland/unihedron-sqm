import math
from datetime import datetime, timezone

import pytest

from sqm_service import astro


def ts(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def test_julian_day_epoch():
    assert astro.julian_day(ts(2000, 1, 1, 12)) == pytest.approx(2451545.0)


def test_sidereal_time_meeus_12a():
    # 1987 April 10, 0h UT: 13h10m46.3668s
    jd = astro.julian_day(ts(1987, 4, 10))
    assert astro.sidereal_degrees(jd) == pytest.approx(197.693195, abs=1e-4)


def test_sun_position_meeus_25a():
    # 1992 October 13.0: RA 13h13m31.4s, Dec -7°47'06"
    ra, dec = astro.sun_equatorial(2448908.5)
    assert ra == pytest.approx(198.38083, abs=0.01)
    assert dec == pytest.approx(-7.78507, abs=0.01)


def test_moon_position_meeus_47a():
    # 1992 April 12, 0h: RA 134.688470°, Dec 13.768368°
    ra, dec = astro.moon_equatorial(2448724.5)
    assert ra == pytest.approx(134.688470, abs=0.3)
    assert dec == pytest.approx(13.768368, abs=0.3)


def test_moon_phase_meeus_48a():
    fraction, waxing = astro.moon_phase(ts(1992, 4, 12))
    assert fraction == pytest.approx(0.6786, abs=0.01)
    assert waxing is True


def test_sun_near_zenith_at_equinox_noon_on_equator():
    # 2024 March 20: equinox at 03:06 UT; transit at 0° longitude ~12:07:30 UT.
    assert astro.sun_altitude(ts(2024, 3, 20, 12, 7, 30), 0.0, 0.0) > 89.5
    assert astro.sun_altitude(ts(2024, 3, 21, 0, 7, 30), 0.0, 0.0) < -89.5


def test_full_moon_is_up_at_local_midnight():
    # Full moon 2024 April 23 23:49 UT; at midnight in Greenwich it is high.
    assert astro.moon_altitude(ts(2024, 4, 24), 51.48, 0.0) > 15
    fraction, _ = astro.moon_phase(ts(2024, 4, 24))
    assert fraction > 0.98
