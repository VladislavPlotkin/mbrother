"""Persistent TCP link between the controller and the agent."""
from __future__ import annotations

import queue
import socket
import threading
import time

from ..protocol import MAGIC, ProtocolError, recv_json, send_json


class AgentLink:
    """Request/response plus fire-and-forget messages over one socket."""

    def __init__(self, host: str, port: int = 7777, timeout: float = 8.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue()
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.last_error: str | None = None

    # ----------------------------------------------------------- connection
    def connect(self) -> bool:
        with self._lock:
            self._disconnect()
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall(MAGIC)
                self._sock = sock
                self.connected = True
                self.last_error = None
            except OSError as exc:
                self.last_error = str(exc)
                self.connected = False
                return False
            self._stop.clear()
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            return True

    def _disconnect(self) -> None:
        sock = self._sock
        self._sock = None
        self.connected = False
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._disconnect()

    # -------------------------------------------------------------- reading
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            sock.settimeout(self.timeout)  # detect dead connections
            try:
                while not self._stop.is_set():
                    message = recv_json(sock)
                    if "rid" in message and message["rid"] is not None:
                        waiter = self._pending.pop(message["rid"], None)
                        if waiter is not None:
                            waiter.put(message)
                    else:
                        self._events.put(message)
            except socket.timeout:
                continue  # keep reading
            except (ProtocolError, OSError, ValueError) as exc:
                self.last_error = str(exc)
                with self._lock:
                    self._disconnect()
                for waiter in list(self._pending.values()):
                    waiter.put({"ok": False, "error": "connection lost"})
                # Auto-reconnect after brief pause
                if not self._stop.is_set():
                    time.sleep(2)
                    if not self._stop.is_set():
                        self.connect()
                        break  # outer loop continues with new connection

    def events(self) -> queue.Queue:
        return self._events

    # ------------------------------------------------------------- requests
    def _next_request_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def call(self, cmd: str, timeout: float | None = None, **params) -> dict:
        """Send a command and wait for its answer."""
        if not self.connected and not self.connect():
            raise ConnectionError(self.last_error or "agent is not reachable")
        request_id = self._next_request_id()
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._pending[request_id] = waiter
        message = {"rid": request_id, "cmd": cmd}
        message.update(params)
        try:
            self._send(message)
        except OSError as exc:
            self._pending.pop(request_id, None)
            raise ConnectionError(str(exc))
        try:
            answer = waiter.get(timeout=timeout or self.timeout)
        except queue.Empty:
            self._pending.pop(request_id, None)
            raise TimeoutError("no answer for %r" % cmd)
        if not answer.get("ok"):
            raise RuntimeError(answer.get("error", "command failed"))
        return answer.get("data", {})

    def send(self, cmd: str, **params) -> None:
        """Fire and forget (used for the high frequency input events)."""
        if not self.connected:
            return
        message = {"cmd": cmd}
        message.update(params)
        try:
            self._send(message)
        except OSError:
            self.connected = False

    def _send(self, message: dict) -> None:
        """Serialise the writes - several threads share this socket."""
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        with self._send_lock:
            send_json(sock, message)


def open_video_stream(host: str, port: int, target=None, timeout: float = 8.0):
    """Open a dedicated video connection to the agent.

    Returns the socket; the caller reads binary frames from it.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(MAGIC)
    send_json(sock, {"rid": 1, "cmd": "video.subscribe", "target": target})
    answer = recv_json(sock)
    if not answer.get("ok"):
        sock.close()
        raise RuntimeError(answer.get("error", "cannot subscribe to the video"))
    return sock


__all__ = ["AgentLink", "open_video_stream", "time"]
