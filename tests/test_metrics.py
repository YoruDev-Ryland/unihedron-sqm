from sqm_service import metrics


def test_render_includes_gauges_with_serial_label():
    text = metrics.render({"mpsas": 21.09, "temperature_c": 8.4, "frequency_hz": 3}, "4171", True, 1000.5, 42)
    assert '# TYPE sqm_mpsas gauge' in text
    assert 'sqm_mpsas{serial="4171"} 21.09' in text
    assert 'sqm_bortle_class{serial="4171"} 4' in text
    assert 'sqm_collector_up{serial="4171"} 1' in text
    assert 'sqm_readings_stored{serial="4171"} 42' in text
    assert text.endswith("\n")


def test_missing_values_are_omitted():
    text = metrics.render({"mpsas": 0.0, "temperature_c": 30.0, "frequency_hz": 500000}, "4171", False, None, 0)
    assert "sqm_bortle_class{" not in text and "sqm_nelm{" not in text
    assert "sqm_last_success_timestamp_seconds{" not in text
    assert 'sqm_collector_up{serial="4171"} 0' in text


def test_large_values_keep_full_precision():
    text = metrics.render({"mpsas": 21.09, "temperature_c": 8.4, "frequency_hz": 3}, "4171", True, 1759852800.123, 1_234_567)
    assert 'sqm_last_success_timestamp_seconds{serial="4171"} 1759852800.123' in text
    assert 'sqm_readings_stored{serial="4171"} 1234567' in text
