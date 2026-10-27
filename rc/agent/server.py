"""TCP server of the agent.

Two kinds of connections are supported on the same port:

* control connections - JSON request/response (``{"id": 1, "cmd": "win.list"}``)
* video connections   - after a ``video.subscribe`` command the agent pushes
  raw JPEG frames as binary protocol frames
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import traceback

from ..protocol import MAGIC, ProtocolError, handshake, recv_json, send_binary, send_json
from .api import Api

import logging

logger = logging.getLogger("rc.agent")

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class AgentServer:
    def __init__(self, api: Api, host: str = "0.0.0.0", port: int = 7777):
        self.api = api
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(8)
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle, args=(conn, addr), daemon=True)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:  # pragma: no cover
            pass

    # ------------------------------------------------------------- handlers
    def _handle(self, conn: socket.socket, addr) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Read exactly 4 bytes to check for RC01 magic or HTTP request start.
            peek_buf = b""
            conn.settimeout(4.0)
            try:
                peek_buf = conn.recv(4)
                if not peek_buf:
                    return
            except socket.timeout:
                return
            conn.settimeout(None)

            # Check if this is a WebSocket upgrade request (starts with "GET ")
            peek_str = peek_buf.decode("utf-8", errors="replace").lower()
            if peek_str.startswith("get "):
                # Full HTTP request — need to read headers for WS upgrade check
                conn.settimeout(3.0)
                try:
                    extra = conn.recv(8192)
                    if extra:
                        peek_buf += extra
                except socket.timeout:
                    pass
                except OSError:
                    return
                conn.settimeout(None)
                peek_str = peek_buf.decode("utf-8", errors="replace").lower()
                if "upgrade" in peek_str and "websocket" in peek_str:
                    # WS upgrade — use the already-read buffer
                    ws_conn = self.doUpgradeWS_from_buf(conn, peek_buf)
                    if ws_conn is not None:
                        # Path already stored in self._ws_path
                        ws_path = getattr(self, "_ws_path", "/ws")
                        if "/term.ws" in ws_path:
                            self.serve_ws_term(ws_conn)
                        else:
                            self.serve_ws_video(ws_conn)
                    return

            # Normal protocol path
            peek_str2 = peek_buf.decode("utf-8", errors="replace")
            if peek_str2.startswith("GET "):
                # Looks like HTTP but not WebSocket — close it
                return

            # Magic already verified above (peek == b'RC01'), just read first message
            conn.settimeout(5.0)
            first = recv_json(conn)
            if first.get("cmd") == "video.subscribe":
                self._serve_video(conn, first)
            else:
                self._serve_control(conn, first)
        except (ProtocolError, OSError, ValueError) as e:
            print(f"handle error: {e}", file=__import__("sys").stderr, flush=True)
        except Exception:  # pragma: no cover
            traceback.print_exc()
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve_control(self, conn: socket.socket, first: dict) -> None:
        message = first
        conn.settimeout(30.0)  # detect dead connections after 30s
        while True:
            request_id = message.get("rid")
            try:
                data = self.api.handle(message)
                if request_id is not None:
                    send_json(conn, {"rid": request_id, "ok": True, "data": data})
            except Exception as exc:
                if request_id is not None:
                    send_json(
                        conn,
                        {"rid": request_id, "ok": False, "error": str(exc)},
                    )
            try:
                message = recv_json(conn)
            except (socket.timeout, ProtocolError, ValueError):
                return

    def _serve_video(self, conn: socket.socket, message: dict) -> None:
        conn.settimeout(30.0)  # detect dead connections after 30s
        target = message.get("target")
        logger.info("video subscribe target=%s rid=%s", target, message.get("rid"))
        send_json(conn, {"rid": message.get("rid"), "ok": True, "data": {"subscribed": True}})
        subscriber = self.api.capture.subscribe(target)
        frames_sent = 0
        try:
            while True:
                item = subscriber.get(timeout=1.0)
                if item is None:
                    continue
                _, jpeg, _stamp = item
                payload = self.api.capture.crop_for(subscriber, jpeg)
                send_binary(conn, payload)
                frames_sent += 1
                if frames_sent % 10 == 0:
                    logger.info("video sent %d frames", frames_sent)
        except (socket.timeout, ProtocolError, OSError):
            logger.info("video connection closed, sent %d frames", frames_sent)
        finally:
            self.api.capture.unsubscribe(subscriber)

    # ----------------------------------------------------------- WebSocket
    def doUpgradeWS(self, conn: socket.socket) -> socket.socket | None:
        """Perform WebSocket handshake.  Read the HTTP request, validate the
        upgrade, send the 101 Switching Protocols response and return the raw
        socket (already upgraded).  Returns None on failure."""
        # Read the HTTP upgrade request line and headers.
        # We expect something like:  GET /ws HTTP/1.1\r\n
        request_line = self._read_http_line(conn)
        if request_line is None:
            return None
        parts = request_line.split()
        if len(parts) < 2:
            return None
        self._ws_path = parts[1]  # store for serve_ws_video
        return self._doUpgradeWS_complete(conn, request_line, {})

    def doUpgradeWS_from_buf(self, conn: socket.socket, buf: bytes) -> socket.socket | None:
        """Perform WebSocket handshake from an already-read buffer."""
        # Parse the HTTP request from buf
        request_line = None
        headers: dict[str, str] = {}
        lines = buf.split(b"\r\n")
        # First line is the request line
        if not lines:
            return None
        request_line = lines[0].decode("utf-8", errors="replace")
        parts = request_line.split()
        if len(parts) < 2:
            return None
        self._ws_path = parts[1]  # store for serve_ws_video

        for line in lines[1:]:
            decoded = line.decode("utf-8", errors="replace")
            if decoded == "" or decoded == "\r\n" or decoded == "\n":
                break
            try:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()
            except ValueError:
                continue

        return self._doUpgradeWS_complete(conn, request_line, headers)

    def _doUpgradeWS_complete(self, conn: socket.socket, request_line: str, headers: dict[str, str]) -> socket.socket | None:
        """Complete the WS handshake with a request_line and pre-parsed headers."""
        key = headers.get("sec-websocket-key", "")
        if not key:
            return None

        # Compute SHA-1 accept value
        accept_b64 = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()
        ).decode()

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_b64}\r\n"
            "\r\n"
        )
        try:
            conn.sendall(response.encode())
        except OSError:
            return None
        return conn

    def _read_http_line(self, conn: socket.socket) -> str | None:
        """Read a single HTTP header line (without \\r\\n)."""
        buf = b""
        conn.settimeout(3.0)
        while True:
            byte = conn.recv(1)
            if not byte:
                return None
            buf += byte
            if buf.endswith(b"\r\n"):
                return buf[:-2].decode("utf-8", errors="replace")
            if len(buf) > 8192:  # safety limit
                return None

    def serve_ws_video(self, ws: socket.socket) -> None:
        """Serve a WebSocket video stream.

        Opens a raw TCP connection to the capture loop (via the protocol),
        sends a ``video.subscribe`` message, receives the ack, and then
        relays JPEG frames from the capture loop to the WebSocket using
        a simple binary-frame protocol:

            [u8 type=0x02] [u32 length] [jpeg bytes]
        """
        # Parse target from the URL query string: /ws?src=screen or /ws?src=win:0
        path = getattr(self, "_ws_path", "/ws")
        query: dict[str, str] = {}
        if "?" in path:
            qs = path.split("?", 1)[1]
            for part in qs.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query[k] = v

        target = None
        src = query.get("src", "screen")
        if src.startswith("win:"):
            try:
                target = {"win": int(src[4:])}
            except ValueError:
                target = None

        # Open a dedicated socket to the capture loop
        # We connect back to ourselves on the same port, but send a plain
        # binary frame (no magic) — or we use the existing connection.
        # The simplest approach: send subscribe on the existing upgraded
        # WebSocket, and read video frames back over it.
        sub_msg = '{"rid":1,"cmd":"video.subscribe"}'
        # Send as a text frame: 0x81 + payload
        payload = sub_msg.encode("utf-8")
        ws.send(b"\x81" + struct.pack("!H", len(payload)) + payload)

        # Wait for subscribe ack (text frame)
        ws.settimeout(5.0)
        try:
            header = ws.recv(3)
            if len(header) < 3:
                return
            op = header[0] & 0x0F
            plen = header[1] & 0x7F
            if plen == 126:
                plen = struct.unpack("!H", ws.recv(2))[0]
            elif plen == 127:
                plen = struct.unpack("!Q", ws.recv(8))[0]
            ack = ws.recv(plen) if plen > 0 else b""
        except (OSError, socket.timeout):
            return

        # Now relay JPEG frames from capture loop back to WebSocket.
        # We use a dedicated subscriber thread and pump frames.
        stop_event = threading.Event()
        subscriber = self.api.capture.subscribe(target)
        try:
            while not stop_event.is_set():
                item = subscriber.get(timeout=0.5)
                if item is None:
                    continue
                _, jpeg, _stamp = item
                frame_payload = self.api.capture.crop_for(subscriber, jpeg)
                # WebSocket binary frame: FIN + opcode 0x02 (binary), no masking
                # (server->client), RFC 6455 length encoding (7-bit / 16 / 64-bit)
                n = len(frame_payload)
                if n < 126:
                    hdr = bytes([0x82, n])
                elif n < 65536:
                    hdr = bytes([0x82, 126]) + struct.pack("!H", n)
                else:
                    hdr = bytes([0x82, 127]) + struct.pack("!Q", n)
                ws.send(hdr + frame_payload)
        except (OSError, socket.timeout):
            pass
        finally:
            stop_event.set()
            self.api.capture.unsubscribe(subscriber)
            try:
                ws.close()
            except OSError:
                pass

    def serve_ws_term(self, ws: socket.socket) -> None:
        """Serve a WebSocket terminal — proxies PTY input/output.

        The frontend (web/app.js) sends JSON text frames
        ``{"cmd":"term.data","data":"<keys>"}`` and accepts output either as
        raw binary frames or as JSON ``{"cmd":"term.data","data":...}``.
        A relay thread polls ``term.read`` on the local API at ~10 Hz and
        pushes PTY output to the browser; the main loop forwards input.
        """
        import time

        # Parse start size from URL query string
        path = getattr(self, "_ws_path", "/term.ws")
        query: dict[str, str] = {}
        if "?" in path:
            qs = path.split("?", 1)[1]
            for part in qs.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query[k] = v
        start_cols = 160
        start_rows = 60
        try:
            c = int(query.get("cols", "160"))
            if 20 <= c <= 500:
                start_cols = c
        except (ValueError, TypeError):
            pass
        try:
            r = int(query.get("rows", "60"))
            if 5 <= r <= 300:
                start_rows = r
        except (ValueError, TypeError):
            pass

        # Blocking reads; TCP keepalive detects dead peers.
        ws.settimeout(None)
        try:
            ws.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

        # Own terminal on the local API.  term.data is sent without a rid
        # (fire-and-forget — _serve_control skips responses for rid-less
        # requests), so the relay only ever receives term.read responses.
        term_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        term_sock.connect(("127.0.0.1", self.port))
        term_sock.sendall(MAGIC)
        send_json(term_sock, {"rid": 1, "cmd": "term.new", "cols": start_cols, "rows": start_rows})
        ack = recv_json(term_sock)
        tid = (ack.get("data") or {}).get("tid") if ack.get("ok") else None
        if not tid:
            err = json.dumps({"cmd": "term.error", "data": "term.new failed: %r" % ack})
            payload = err.encode("utf-8")
            ws.send(b"\x81" + struct.pack("!H", len(payload)) + payload)
            term_sock.close()
            return

        stop = threading.Event()
        send_lock = threading.Lock()  # relay + main loop share term_sock

        def ws_frame(op: int, payload: bytes) -> bytes:
            """Build a single unmasked server-to-client WS frame."""
            n = len(payload)
            if n < 126:
                hdr = bytes([0x80 | op, n])
            elif n < 65536:
                hdr = bytes([0x80 | op, 126]) + struct.pack("!H", n)
            else:
                hdr = bytes([0x80 | op, 127]) + struct.pack("!Q", n)
            return hdr + payload

        def recv_exact(sock: socket.socket, count: int) -> bytes:
            buf = b""
            while len(buf) < count:
                chunk = sock.recv(count - len(buf))
                if not chunk:
                    raise ConnectionError("ws peer closed")
                buf += chunk
            return buf

        def relay_agent() -> None:
            """Poll term.read at ~10 Hz; push PTY output to the browser."""
            rid = 100
            while not stop.is_set():
                try:
                    rid += 1
                    with send_lock:
                        send_json(term_sock, {"rid": rid, "cmd": "term.read", "tid": tid})
                    resp = recv_json(term_sock)
                except (ProtocolError, OSError, ValueError) as _e:
                    break
                data = resp.get("data")
                out = data.get("data", "") if isinstance(data, dict) else ""
                if out:
                    try:
                        ws.send(ws_frame(0x2, out.encode("utf-8", errors="replace")))
                    except OSError:
                        break
                time.sleep(0.05)

        threading.Thread(target=relay_agent, daemon=True).start()

        try:
            while not stop.is_set():
                header = recv_exact(ws, 2)
                op = header[0] & 0x0F
                b1 = header[1]
                plen = b1 & 0x7F
                if plen == 126:
                    plen = struct.unpack("!H", recv_exact(ws, 2))[0]
                elif plen == 127:
                    plen = struct.unpack("!Q", recv_exact(ws, 8))[0]
                mk = recv_exact(ws, 4) if (b1 & 0x80 and plen) else None
                data = recv_exact(ws, plen) if plen else b""
                if mk:
                    data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
                if op == 0x8:  # close
                    break
                if op == 0x9:  # ping -> pong
                    ws.send(b"\x8a" + bytes([len(data)]) + data)
                    continue
                if op not in (0x1, 0x2) or not data:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if msg.get("cmd") == "term.data" and msg.get("data"):
                    with send_lock:
                        send_json(term_sock, {"cmd": "term.data", "tid": tid, "data": msg["data"]})
                elif msg.get("cmd") == "term.resize":
                    cols = rows = None
                    try:
                        c = int(msg.get("cols", 0))
                        r = int(msg.get("rows", 0))
                        if 20 <= c <= 500 and 5 <= r <= 300:
                            cols, rows = c, r
                    except (ValueError, TypeError):
                        pass
                    if cols is not None:
                        with send_lock:
                            send_json(term_sock, {"cmd": "term.resize", "tid": tid, "cols": cols, "rows": rows})
                            # Consume the API response so relay_thread doesn't eat it
                            try:
                                resp = recv_json(term_sock, timeout=1)
                                rid_resp = resp.get("rid")
                                if rid_resp is not None:
                                    # Send the response back to the browser panel
                                    rsp = json.dumps(resp)
                                    ws.send(ws_frame(0x1, rsp.encode("utf-8")))
                            except (ProtocolError, OSError, ValueError, TimeoutError):
                                pass
        except (OSError, ConnectionError, ProtocolError, ValueError):
            pass
        finally:
            stop.set()
            try:
                term_sock.close()
            except OSError:
                pass
            try:
                ws.close()
            except OSError:
                pass
