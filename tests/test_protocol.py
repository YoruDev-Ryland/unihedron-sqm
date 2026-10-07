from sqm_service.discovery import parse_discovery_reply
from sqm_service.sqm_client import SQMError, parse_info, parse_reading


def test_parse_reading():
    reading = parse_reading(
        "r, 21.34m,0000000012Hz,0000000000c,0000000.000s, 012.4C"
    )
    assert reading["mpsas"] == 21.34
    assert reading["frequency_hz"] == 12
    assert reading["temperature_c"] == 12.4


def test_parse_info():
    info = parse_info("i,00000004,00000003,00000080,00004171")
    assert info == {
        "protocol": 4,
        "model": 3,
        "feature": 80,
        "serial": 4171,
        "raw": "i,00000004,00000003,00000080,00004171",
    }


def test_bad_protocol_response():
    try:
        parse_reading("not a reading")
    except SQMError:
        pass
    else:
        raise AssertionError("invalid response was accepted")


def test_discovery_reply_mac_location():
    packet = bytearray(40)
    packet[:4] = b"\x00\x00\x00\xf7"
    packet[24:30] = bytes.fromhex("00204AABCDEF")
    parsed = parse_discovery_reply(bytes(packet), ("192.0.2.9", 30718))
    assert parsed == {
        "host": "192.0.2.9",
        "mac": "00:20:4A:AB:CD:EF",
        "source": "udp",
    }

