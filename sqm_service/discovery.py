"""Discover SQM-LE devices using the protocol documented by Unihedron."""

from __future__ import annotations

import ipaddress
import socket
import time

DISCOVERY_PORT = 30718
DISCOVERY_QUERY = b"\x00\x00\x00\xf6"
DISCOVERY_REPLY = b"\x00\x00\x00\xf7"


def parse_discovery_reply(packet: bytes, address: tuple[str, int]) -> dict | None:
    if len(packet) < 30 or not packet.startswith(DISCOVERY_REPLY):
        return None
    mac_bytes = packet[24:30]
    return {
        "host": address[0],
        "mac": ":".join(f"{part:02X}" for part in mac_bytes),
        "source": "udp",
    }


def broadcast_discover(
    broadcast_address: str = "255.255.255.255", timeout: float = 2.0
) -> list[dict]:
    try:
        ipaddress.ip_address(broadcast_address)
    except ValueError as exc:
        raise ValueError("broadcast_address must be an IPv4 address") from exc

    found: dict[str, dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sock.settimeout(0.2)
        sock.sendto(DISCOVERY_QUERY, (broadcast_address, DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet, address = sock.recvfrom(2048)
            except socket.timeout:
                continue
            parsed = parse_discovery_reply(packet, address)
            if parsed:
                found[parsed["host"]] = parsed
    finally:
        sock.close()
    return sorted(found.values(), key=lambda item: ipaddress.ip_address(item["host"]))


def hosts_in_subnet(cidr: str) -> list[str]:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise ValueError("scan subnet must be valid CIDR, for example 192.168.1.0/24") from exc
    if network.version != 4:
        raise ValueError("only IPv4 discovery is supported")
    if network.num_addresses > 256:
        raise ValueError("scan subnet is limited to 256 addresses (/24)")
    return [str(address) for address in network.hosts()]

