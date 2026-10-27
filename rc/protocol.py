"""Wire protocol shared by the rc agent and the rc client.

Frame layout (network byte order):

    [u8 type][u32 length][payload]

``T_JSON`` (0x01)   payload is a UTF-8 encoded JSON object
``T_BINARY`` (0x02) payload is raw bytes (used for JPEG video frames)

A session starts with the 4 byte magic ``RC01``.  It is *not* a security
feature - no encryption, no authentication, as requested.  It exists only so
that a random port scanner hitting the control port does not get to power
cycle somebody's computer.
"""
from __future__ import annotations

import json
import socket
import struct

T_JSON = 0x01
T_BINARY = 0x02

MAGIC = b"RC01"
_HEADER = struct.Struct("!BI")


class ProtocolError(Exception):
    """Raised on a malformed frame or a failed handshake."""


def encode(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return _HEADER.pack(T_JSON, len(payload)) + payload


def encode_binary(data: bytes) -> bytes:
    return _HEADER.pack(T_BINARY, len(data)) + data


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ProtocolError("connection closed by peer")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_raw(sock: socket.socket) -> tuple[int, bytes]:
    header = _recv_exactly(sock, _HEADER.size)
    mtype, length = _HEADER.unpack(header)
    if length > 256 * 1024 * 1024:
        raise ProtocolError("frame too large: %d" % length)
    payload = _recv_exactly(sock, length) if length else b""
    return mtype, payload


def recv_json(sock: socket.socket) -> dict:
    mtype, payload = recv_raw(sock)
    if mtype != T_JSON:
        raise ProtocolError("expected a JSON frame, got type %d" % mtype)
    return json.loads(payload.decode("utf-8"))


def send_json(sock: socket.socket, message: dict) -> None:
    sock.sendall(encode(message))


def send_binary(sock: socket.socket, data: bytes) -> None:
    sock.sendall(encode_binary(data))


def handshake(sock: socket.socket, timeout: float = 5.0) -> None:
    """Read and validate the session magic."""
    sock.settimeout(timeout)
    magic = _recv_exactly(sock, len(MAGIC))
    if magic != MAGIC:
        raise ProtocolError("bad magic %r" % magic)


def send_magic(sock: socket.socket) -> None:
    sock.sendall(MAGIC)
