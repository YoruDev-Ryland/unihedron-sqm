import pytest

from sqm_service import sky


@pytest.mark.parametrize(
    ("mpsas", "bortle"),
    [
        (22.0, 1),
        (21.99, 1),
        (21.95, 2),
        (21.75, 3),
        (21.09, 4),
        (20.0, 5),
        (19.2, 6),
        (18.6, 7),
        (18.2, 8),
        (17.0, 9),
    ],
)
def test_bortle_class(mpsas, bortle):
    assert sky.bortle(mpsas) == bortle


def test_nelm_matches_published_formula():
    assert sky.nelm(21.5) == pytest.approx(6.38, abs=0.01)
    assert sky.nelm(18.0) == pytest.approx(3.97, abs=0.01)


def test_twilight_and_daylight_have_no_sky_class():
    assert sky.bortle(15.9) is None
    assert sky.nelm(0.0) is None
    assert sky.describe(12.0) == {"bortle": None, "bortle_name": None, "nelm": None}


def test_describe_names_the_class():
    assert sky.describe(21.09) == {
        "bortle": 4,
        "bortle_name": "Rural/suburban transition",
        "nelm": 6.17,
    }
