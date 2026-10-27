"""Wake up the controlled machine.

The target PC uses an Atheros AR8161 with the ``alx`` driver, which does not
implement Wake-on-LAN, so the classic magic packet may not reach a powered
off machine.  Two back ends are provided anyway:

``magic`` - UDP magic packet (works as soon as the NIC/driver supports it)
``plug``  - HTTP request to a smart plug / relay that switches the power
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path


def magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> bytes:
    cleaned = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(cleaned) != 12:
        raise ValueError("bad MAC address %r" % mac)
    raw = bytes.fromhex(cleaned)
    return b"\xff" * 6 + raw * 16


def send_magic(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    packet = magic_packet(mac, broadcast, port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))


def plug(url: str, timeout: float = 6.0) -> str:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(512).decode("utf-8", "replace")
            return "HTTP %s: %s" % (response.status, body.strip()[:200])
    except urllib.error.HTTPError as exc:
        return "HTTP %s: %s" % (exc.code, exc.reason)
    except Exception as exc:
        return "error: %s" % exc


def load_settings(path: str | Path | None = None) -> dict:
    path = Path(path).expanduser() if path else Path.home() / ".config" / "rc" / "client.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def wake(settings: dict | None = None) -> str:
    """Wake the target machine using the configured back end."""
    settings = settings if settings is not None else load_settings()
    method = settings.get("method", "magic")
    if method == "plug":
        url = settings.get("plug_url")
        if not url:
            raise RuntimeError("plug_url is not configured")
        return plug(url)
    mac = settings.get("mac")
    if not mac:
        raise RuntimeError("MAC address is not configured")
    broadcast = settings.get("broadcast", "255.255.255.255")
    send_magic(mac, broadcast, int(settings.get("port", 9)))
    return "magic packet sent to %s via %s" % (mac, broadcast)
