"""Low-precision sun and moon positions (Meeus, Astronomical Algorithms).

Accurate to roughly 0.01° for the sun and 0.3° for the moon, which is far more
than needed to decide whether the sun is below −18° or the moon is up.
"""

from __future__ import annotations

import math

J2000 = 2451545.0
MOON_PARALLAX = 0.9507  # mean horizontal parallax, degrees


def _sin(deg: float) -> float:
    return math.sin(math.radians(deg))


def _cos(deg: float) -> float:
    return math.cos(math.radians(deg))


def julian_day(ts: float) -> float:
    return ts / 86400.0 + 2440587.5


def sidereal_degrees(jd: float) -> float:
    t = (jd - J2000) / 36525
    return (280.46061837 + 360.98564736629 * (jd - J2000)
            + 0.000387933 * t * t - t ** 3 / 38710000) % 360


def _obliquity(t: float) -> float:
    return 23.4392911 - (46.8150 * t + 0.00059 * t * t - 0.001813 * t ** 3) / 3600


def _to_equatorial(longitude: float, latitude: float, epsilon: float) -> tuple[float, float]:
    ra = math.degrees(math.atan2(
        _sin(longitude) * _cos(epsilon) - math.tan(math.radians(latitude)) * _sin(epsilon),
        _cos(longitude),
    )) % 360
    dec = math.degrees(math.asin(
        _sin(latitude) * _cos(epsilon) + _cos(latitude) * _sin(epsilon) * _sin(longitude)
    ))
    return ra, dec


def _sun_longitude(t: float) -> tuple[float, float]:
    """Apparent ecliptic longitude of the sun and the nutation node Ω."""
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    centre = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * _sin(m)
              + (0.019993 - 0.000101 * t) * _sin(2 * m) + 0.000289 * _sin(3 * m))
    omega = 125.04 - 1934.136 * t
    return (l0 + centre - 0.00569 - 0.00478 * _sin(omega)) % 360, omega


def sun_equatorial(jd: float) -> tuple[float, float]:
    t = (jd - J2000) / 36525
    longitude, omega = _sun_longitude(t)
    return _to_equatorial(longitude, 0.0, _obliquity(t) + 0.00256 * _cos(omega))


def _moon_arguments(t: float) -> tuple[float, float, float, float, float]:
    lp = 218.3164477 + 481267.88123421 * t
    d = 297.8501921 + 445267.1114034 * t
    m = 357.5291092 + 35999.0502909 * t
    mp = 134.9633964 + 477198.8675055 * t
    f = 93.2720950 + 483202.0175233 * t
    return lp, d, m, mp, f


def _moon_ecliptic(t: float) -> tuple[float, float]:
    lp, d, m, mp, f = _moon_arguments(t)
    e = 1 - 0.002516 * t
    longitude = lp + (
        6.288774 * _sin(mp) + 1.274027 * _sin(2 * d - mp) + 0.658314 * _sin(2 * d)
        + 0.213618 * _sin(2 * mp) - 0.185116 * e * _sin(m) - 0.114332 * _sin(2 * f)
        + 0.058793 * _sin(2 * d - 2 * mp) + 0.057066 * e * _sin(2 * d - m - mp)
        + 0.053322 * _sin(2 * d + mp) + 0.045758 * e * _sin(2 * d - m)
        - 0.040923 * e * _sin(m - mp) - 0.034720 * _sin(d) - 0.030383 * e * _sin(m + mp)
    )
    latitude = (
        5.128122 * _sin(f) + 0.280602 * _sin(mp + f) + 0.277693 * _sin(mp - f)
        + 0.173237 * _sin(2 * d - f) + 0.055413 * _sin(2 * d - mp + f)
        + 0.046271 * _sin(2 * d - mp - f) + 0.032573 * _sin(2 * d + f)
    )
    return longitude % 360, latitude


def moon_equatorial(jd: float) -> tuple[float, float]:
    t = (jd - J2000) / 36525
    longitude, latitude = _moon_ecliptic(t)
    return _to_equatorial(longitude, latitude, _obliquity(t))


def altitude(ra: float, dec: float, jd: float, lat: float, lon: float) -> float:
    hour_angle = sidereal_degrees(jd) + lon - ra
    return math.degrees(math.asin(
        _sin(lat) * _sin(dec) + _cos(lat) * _cos(dec) * _cos(hour_angle)
    ))


def sun_altitude(ts: float, lat: float, lon: float) -> float:
    jd = julian_day(ts)
    return altitude(*sun_equatorial(jd), jd, lat, lon)


def moon_altitude(ts: float, lat: float, lon: float) -> float:
    jd = julian_day(ts)
    geocentric = altitude(*moon_equatorial(jd), jd, lat, lon)
    return geocentric - MOON_PARALLAX * _cos(geocentric)


def moon_phase(ts: float) -> tuple[float, bool]:
    t = (julian_day(ts) - J2000) / 36525
    _, d, m, mp, _ = _moon_arguments(t)
    phase_angle = (180 - d - 6.289 * _sin(mp) + 2.100 * _sin(m) - 1.274 * _sin(2 * d - mp)
                   - 0.658 * _sin(2 * d) - 0.214 * _sin(2 * mp) - 0.110 * _sin(d))
    fraction = (1 + _cos(phase_angle)) / 2
    elongation = (_moon_ecliptic(t)[0] - _sun_longitude(t)[0]) % 360
    return fraction, elongation < 180
