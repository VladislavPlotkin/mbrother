"""The controller side application.

It runs a small HTTP server on the machine that does the controlling and
serves two pages:

``/``       the control panel (windows, processes, power, clipboard, files)

The panel lives on the controller on purpose: the agent on the target is dead
while the target is powered off, so the "power on" button has to be served
from here.

There is deliberately NO video page in the browser: the separate video window
is the native viewer (``rc viewer``), started via POST /api/viewer.open.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import mimetypes
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..protocol import MAGIC, encode, recv_json
from .agent_link import AgentLink
from .wake import load_settings, wake as do_wake

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_logger_h = logging.StreamHandler()
_logger_h.setLevel(logging.INFO)
logger.addHandler(_logger_h)


def iter_chunked(rfile):
    """Decode an HTTP/1.1 chunked request body and yield the raw chunks."""
    while True:
        line = rfile.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            return
        if size == 0:
            while True:
                trailer = rfile.readline()
                if trailer in (b"\r\n", b"\n", b""):
                    break
            return
        data = rfile.read(size)
        rfile.read(2)
        yield data


class ControlApp:
    """Shared state of the local server."""

    def __init__(
        self,
        agent_host: str,
        agent_port: int = 7777,
        settings: dict | None = None,
        settings_path: str | os.PathLike | None = None,
    ):
        self.agent_host = agent_host
        self.agent_port = agent_port
        self.settings = settings if settings is not None else load_settings()
        self.settings_path = Path(settings_path).expanduser() if settings_path else None
        self.link = AgentLink(agent_host, agent_port)
        self.started = time.time()
        self.input_events = 0
        self.input_last = None  # (timestamp, command)
        self._terminals = {}  # tid -> (fd, event)

    def connect(self) -> bool:
        ok = self.link.connect()
        if ok:
            self._learn_mac()
        return ok

    def _learn_mac(self) -> None:
        """Remember the MAC of the target - it is needed to wake it up."""
        if self.settings.get("mac"):
            return
        try:
            info = self.link.call("ping", timeout=5)
        except Exception:
            return
        mac = info.get("mac")
        if not mac:
            return
        self.settings["mac"] = mac
        if self.settings_path is None:
            return
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(self.settings, indent=2, ensure_ascii=False) + "\n"
            )
            print("MAC цели (%s) сохранён в %s" % (mac, self.settings_path), flush=True)
        except OSError:
            pass

    def _proxy_json(self, cmd: str, params: dict) -> dict:
        """Send a JSON command to the agent and return the response."""
        return self.link.call(cmd, **params)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rc/1.0"

    # ------------------------------------------------------------- helpers
    @property
    def app(self) -> ControlApp:
        return self.server._control_app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # silence the default stderr logging
        if os.environ.get("RC_VERBOSE"):
            print("[rc] " + fmt % args, flush=True)

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 502) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = os.path.basename(path)
                return self._send_file(WEB_ROOT / name)
            if path == "/download":
                return self._download(query)
            if path == "/api/status":
                return self._send_json({"ok": True, "data": self._status()})
            if path.startswith("/api/"):
                cmd = path[len("/api/"):]
                params = {key: value[0] for key, value in query.items()}
                return self._proxy_call(cmd, params)
            self.send_error(404)
        except BrokenPipeError:  # pragma: no cover
            pass
        except Exception as exc:
            traceback.print_exc()
            self._send_error_json(str(exc))

    def _status(self) -> dict:
        link = self.app.link
        agent = {"connected": link.connected, "error": link.last_error}
        try:
            ping = link.call("ping", timeout=3)
            agent["info"] = ping
            agent["connected"] = True
        except Exception as exc:
            agent["error"] = str(exc)
        last = self.app.input_last
        return {
            "agent": agent,
            "host": self.app.agent_host,
            "port": self.app.agent_port,
            "uptime": int(time.time() - self.app.started),
            "input": {
                "events": self.app.input_events,
                "last": last[1] if last else None,
                "age": round(time.time() - last[0], 2) if last else None,
            },
        }

    def _proxy_call(self, cmd: str, params: dict) -> None:
        if cmd.startswith("input."):
            self.app.input_events += 1
            self.app.input_last = (time.time(), cmd)
        try:
            data = self.app.link.call(cmd, **params)
            self._send_json({"ok": True, "data": data})
        except Exception as exc:
            self._send_error_json(str(exc))

    def _download(self, query: dict) -> None:
        """Fetch a file from the agent and hand it over as a download."""
        name = (query.get("name") or [""])[0]
        folder = (query.get("folder") or ["inbox"])[0]
        if not name:
            return self._send_error_json("no file name given", 400)
        try:
            data = self.app.link.call("file.read", name=name, folder=folder)
        except Exception as exc:
            return self._send_error_json(str(exc))
        try:
            body = base64.b64decode(data.get("data", ""))
        except Exception as exc:
            return self._send_error_json("corrupt file data: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            'attachment; filename="%s"' % os.path.basename(name).replace('"', ""),
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/input":
                return self._input_stream()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if path.startswith("/api/"):
                cmd = path[len("/api/"):]
                params = {}
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                        if isinstance(body, dict):
                            params = body
                    except ValueError:
                        params = {"raw": raw.decode("utf-8", "replace")}
                if cmd == "wake":
                    try:
                        return self._send_json({"ok": True, "data": {"result": do_wake(self.app.settings)}})
                    except Exception as exc:
                        return self._send_error_json(str(exc))
                if cmd == "viewer.open":
                    try:
                        return self._send_json({"ok": True, "data": self._open_viewer(params)})
                    except Exception as exc:
                        return self._send_error_json(str(exc))
                return self._proxy_call(cmd, params)
            if path == "/upload":
                name = (query.get("name") or ["file"])[0]
                folder = (query.get("folder") or ["inbox"])[0]
                return self._proxy_call("file.put", {"name": name, "folder": folder, "data": _b64(raw)})
            self.send_error(404)
        except BrokenPipeError:  # pragma: no cover
            pass
        except Exception as exc:
            traceback.print_exc()
            self._send_error_json(str(exc))

    def _open_viewer(self, params: dict) -> dict:
        """Start the native viewer window on this (the controller) machine."""
        source = str(params.get("source") or "screen")
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        auth = Path("~/.Xauthority").expanduser()
        if auth.exists():
            env.setdefault("XAUTHORITY", str(auth))
        command = [sys.executable, "-m", "rc", "viewer", "--source", source]
        if params.get("fullscreen"):
            command.append("--fullscreen")
        proc = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"pid": proc.pid, "source": source}

    def _input_stream(self) -> None:
        """Receive newline delimited JSON events and forward them to the agent."""
        length = self.headers.get("Content-Length")
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if length:
            chunks = [self.rfile.read(int(length))]
        elif "chunked" in encoding:
            chunks = iter_chunked(self.rfile)
        else:
            chunks = [self.rfile.read()]
        pending = b""
        forwarded = 0
        for chunk in chunks:
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                cmd = event.pop("t", None) or event.pop("cmd", None)
                if not cmd:
                    continue
                self.app.link.send(cmd, **event)
                self.app.input_events += 1
                self.app.input_last = (time.time(), cmd)
                forwarded += 1
        self._send_json({"ok": True, "data": {"events": forwarded}})

    # ----------------------------------------------------------- WebSocket
    def doUpgradeWS(self) -> socket.socket | None:
        """Upgrade to WebSocket (simple WS key-based handshake)."""
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return None
        try:
            import hashlib
            magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept = hashlib.sha1((key + magic).encode()).digest().hex().upper()
            accept_b = bytes.fromhex(accept).hex()
            # We'll use base64 but the hex version also works with most clients
            import base64 as b64mod
            accept_b64 = b64mod.b64encode(hashlib.sha1((key + magic).encode()).digest()).decode()
        except Exception:
            return None

        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_b64)
        self.end_headers()
        # Get the underlying socket from the file object
        try:
            return self.wfile.raw._sock  # type: ignore[union-attr]
        except AttributeError:
            return self.connection

    def serve_ws_term(self):
        """WebSocket terminal endpoint.

        The agent never pushes PTY output - it only answers ``term.read``
        polls.  A single thread therefore alternates between reading browser
        input (select on the WS socket) and polling the agent every ~100 ms,
        forwarding PTY output to the browser as JSON text frames:
        ``{"cmd":"term.data","tid":...,"data":"<output>"}``.
        """
        import itertools
        import select as select_mod
        import struct

        ws_sock = None
        agent_sock = None
        tid = None
        rid_gen = itertools.count(1)

        # Parse start size from query string
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        start_cols = 160
        start_rows = 60
        try:
            c = int(qs.get("cols", ["160"])[0])
            if 20 <= c <= 500:
                start_cols = c
        except (ValueError, IndexError):
            pass
        try:
            r = int(qs.get("rows", ["60"])[0])
            if 5 <= r <= 300:
                start_rows = r
        except (ValueError, IndexError):
            pass

        def rpc(sock, msg, timeout=2.0):
            """Send one request; return the response with a matching rid."""
            rid = next(rid_gen)
            sock.sendall(encode(dict(msg, rid=rid)))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("agent response timeout for %s" % msg.get("cmd"))
                sock.settimeout(remaining)
                resp = recv_json(sock)
                if resp.get("rid") == rid:
                    return resp

        def ws_send_text(sock, text):
            raw = text.encode("utf-8")
            n = len(raw)
            if n < 126:
                hdr = b"\x81" + bytes([n])
            elif n < 65536:
                hdr = b"\x81\x7e" + struct.pack("!H", n)
            else:
                hdr = b"\x81\x7f" + struct.pack("!Q", n)
            sock.sendall(hdr + raw)

        try:
            agent_sock = socket.create_connection(
                (self.app.agent_host, self.app.agent_port), timeout=5.0
            )
            agent_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            agent_sock.sendall(MAGIC)
            ack = rpc(agent_sock, {"cmd": "term.new", "cols": start_cols, "rows": start_rows})
            tid = ack.get("data", {}).get("tid") if ack.get("ok") else None
            if not tid:
                logger.error("term: term.new failed: %r", ack)
                return

            ws_sock = self.doUpgradeWS()
            if ws_sock is None:
                logger.error("term: ws upgrade failed (no key?)")
                return
            ws_sock.settimeout(None)

            last_poll = 0.0

            def recv_exact(sock, count):
                """Read exactly count bytes (recv may return fewer)."""
                buf = b""
                while len(buf) < count:
                    chunk = sock.recv(count - len(buf))
                    if not chunk:
                        raise ConnectionError("ws peer closed")
                    buf += chunk
                return buf

            while True:
                ready, _, _ = select_mod.select([ws_sock], [], [], 0.05)
                if ready:
                    header = recv_exact(ws_sock, 2)
                    op = header[0] & 0x0F
                    b1 = header[1]
                    if op == 0x8:  # close
                        break
                    plen = b1 & 0x7F
                    if plen == 126:
                        plen = struct.unpack("!H", recv_exact(ws_sock, 2))[0]
                    elif plen == 127:
                        plen = struct.unpack("!Q", recv_exact(ws_sock, 8))[0]
                    mk = recv_exact(ws_sock, 4) if (b1 & 0x80 and plen) else None
                    data = recv_exact(ws_sock, plen) if plen else b""
                    if mk:
                        data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
                    if op == 0x9:  # ping -> pong
                        ws_sock.sendall(b"\x8a" + bytes([len(data)]) + data)
                        continue
                    if op not in (0x1, 0x2) or not data:  # text / binary
                        if data:
                            logger.info("term: ignoring op=%d len=%d", op, len(data))
                        continue
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        logger.warning("term: bad ws frame: op=%d data=%r exc=%s", op, data[:60], exc)
                        continue
                    if msg.get("cmd") == "term.data" and msg.get("data"):
                        try:
                            logger.info("term: received input %r from browser", msg["data"])
                            rpc(agent_sock, {"cmd": "term.data", "tid": tid, "data": msg["data"]})
                        except Exception as exc:
                            logger.error("term input error: %s", exc)
                            break
                    if msg.get("cmd") == "term.resize":
                        cols = rows = None
                        try:
                            c = int(msg.get("cols", 0))
                            r = int(msg.get("rows", 0))
                            if 20 <= c <= 500 and 5 <= r <= 300:
                                cols, rows = c, r
                        except (ValueError, TypeError):
                            pass
                        if cols is not None:
                            try:
                                rpc(agent_sock, {"cmd": "term.resize", "tid": tid, "cols": cols, "rows": rows})
                            except Exception as exc:
                                logger.error("term resize error: %s", exc)
                        continue

                # Poll PTY output from the agent (~10 times per second)
                now = time.monotonic()
                if now - last_poll >= 0.05:
                    last_poll = now
                    try:
                        resp = rpc(agent_sock, {"cmd": "term.read", "tid": tid})
                    except (socket.timeout, TimeoutError, OSError, ValueError) as exc:
                        logger.error("term: poll error: %s", exc)
                        break
                    data_part = resp.get("data")
                    out = data_part.get("data", "") if isinstance(data_part, dict) else (data_part or "")
                    logger.info("term: poll data_len=%d data=%r", len(out), out[:80])
                    if out:
                        logger.info("term: polling output %r", out[:80])
                        ws_send_text(
                            ws_sock,
                            json.dumps({"cmd": "term.data", "tid": tid, "data": out}),
                        )
        except Exception as exc:
            logger.error("term WS error: %s", exc)
            if ws_sock is not None:
                try:
                    ws_send_text(
                        ws_sock,
                        json.dumps({"cmd": "term.error", "data": str(exc)}),
                    )
                except Exception:
                    pass
        finally:
            if agent_sock is not None and tid is not None:
                try:
                    rpc(agent_sock, {"cmd": "term.close", "tid": tid}, timeout=1.0)
                except Exception:
                    pass
            try:
                if ws_sock is not None:
                    ws_sock.close()
            except Exception:
                pass
            try:
                if agent_sock is not None:
                    agent_sock.close()
            except Exception:
                pass

class ControlServer(ThreadingHTTPServer, Handler):
    daemon_threads = True
    allow_reuse_address = True
    disable_nagle_algorithm = True

    def __init__(self, address, handler, app: ControlApp):
        self._control_app = app
        super().__init__(address, handler)

    def serve_forever(self, poll_interval=0.5):
        """Override to call handle_request() so WebSocket upgrades work."""
        import selectors
        from socketserver import _ServerSelector
        self._BaseServer__is_shut_down.clear()
        try:
            with _ServerSelector() as selector:
                selector.register(self, selectors.EVENT_READ)
                while not self._BaseServer__shutdown_request:
                    ready = selector.select(poll_interval)
                    if self._BaseServer__shutdown_request:
                        break
                    if ready:
                        # Handle each connection in its own thread so a long
                        # lived WebSocket (terminal/video) does not block the
                        # panel's regular HTTP polling.
                        threading.Thread(target=self.handle_request, daemon=True).start()
                    self.service_actions()
        finally:
            self._BaseServer__shutdown_request = False
            self._BaseServer__is_shut_down.set()

    def do_GET(self):
        """Delegate GET requests to Handler."""
        return Handler.do_GET(self)

    def do_POST(self):
        """Delegate POST requests to Handler."""
        return Handler.do_POST(self)

    def handle_request(self):
        """Accept one connection; handle it on a per-connection clone.

        The handler state (connection, rfile/wfile, path, headers) lives on
        the instance, so each connection gets a shallow copy of the server -
        this makes handling safe to run concurrently in threads.
        """
        request, client_address = self.get_request()
        h = copy.copy(self)
        h.server = self  # BaseHTTPRequestHandler expects self.server
        h._handle_client(request, client_address)

    def _handle_client(self, request, client_address):
        """Handle one accepted client connection (runs on a clone)."""
        self.request = request
        self.client_address = client_address
        self.connection = request
        # Create rfile/wfile like StreamRequestHandler.setup()
        self.rfile = self.connection.makefile('rb', self.rbufsize if hasattr(self, 'rbufsize') else -1)
        self.wfile = self.connection.makefile('wb', self.wbufsize if hasattr(self, 'wbufsize') else -1)
        # Read the request line
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(414)
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        # Check if this is a WebSocket upgrade
        if self.headers.get("Upgrade", "").lower() == "websocket":
            path = self.path.lower()
            if path.startswith("/term.ws"):
                self.serve_ws_term()
                self.close_connection = True
                return
        # Regular HTTP request — dispatch via do_GET / do_POST
        mname = "do_" + self.command
        if hasattr(self, mname):
            getattr(self, mname)()
        else:
            self.send_error(405)
        self.finish()

    def finish(self):
        """Override finish to properly close the socket."""
        try:
            self.wfile.flush()
        except Exception:
            pass
        self.connection.close()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def serve(agent_host: str, agent_port: int = 7777, bind: str = "127.0.0.1", port: int = 8099,
          settings: dict | None = None, settings_path: str | os.PathLike | None = None) -> None:
    app = ControlApp(agent_host, agent_port, settings, settings_path)
    app.connect()
    server = ControlServer((bind, port), Handler, app)

    def _keep_alive():
        while True:
            time.sleep(3)
            if not app.link.connected:
                app.connect()

    threading.Thread(target=_keep_alive, daemon=True).start()
    print(
        "rc control panel on http://%s:%d  ->  agent %s:%d"
        % (bind, port, agent_host, agent_port),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        app.link.close()


__all__ = ["serve", "ControlApp", "socket"]
