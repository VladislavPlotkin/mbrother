"""Command handlers of the agent.

Every command is a small dictionary; the TCP server and the controller proxy
both funnel into :meth:`Api.handle`.
"""
from __future__ import annotations

import base64
import codecs
import fcntl
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time

from .capture import Capture
from .clip import Clip
from .files import FileStore
from .power import Power
from .procs import Procs
from .x11 import X11, X11Error


class ApiError(Exception):
    """An error that is safe to show to the user."""


def primary_mac() -> str:
    """MAC address of the first real network interface.

    The controller needs it for the Wake-on-LAN magic packet, so the agent
    reports it in the ``ping`` answer and the client stores it in its
    settings.
    """
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            try:
                with open("/sys/class/net/%s/address" % name) as handle:
                    mac = handle.read().strip()
            except OSError:
                continue
            if mac and set(mac) != {"0"}:
                return mac
    except OSError:
        pass
    return ""


class Api:
    def __init__(
        self,
        x11: X11 | None = None,
        capture: Capture | None = None,
        procs: Procs | None = None,
        power: Power | None = None,
        clip: Clip | None = None,
        files: FileStore | None = None,
        version: str = "1.0.0",
    ):
        self.x11 = x11 if x11 is not None else X11()
        self.capture = capture if capture is not None else Capture(self.x11)
        self.procs = procs if procs is not None else Procs()
        self.power = power if power is not None else Power()
        self.clip = clip if clip is not None else Clip()
        self.files = files if files is not None else FileStore()
        self.version = version
        self.started = time.time()
        self._active_window: int | None = None
        self._terminals: dict = {}
        self._term_lock = threading.Lock()

    # ------------------------------------------------------------- dispatch
    def handle(self, message: dict) -> dict:
        cmd = message.get("cmd") or message.get("t")
        handler = getattr(self, "_cmd_" + str(cmd).replace(".", "_"), None)
        if handler is None:
            raise ApiError("unknown command %r" % cmd)
        return handler(message)

    # ------------------------------------------------------------- commands
    def _cmd_ping(self, message: dict) -> dict:
        return {
            "pong": True,
            "version": self.version,
            "host": socket.gethostname(),
            "user": self.procs.username,
            "display": self.x11.name,
            "uptime": int(time.time() - self.started),
            "time": time.time(),
            "screen": self.x11.screen_info(),
            "mac": primary_mac(),
        }

    def _cmd_screen_info(self, message: dict) -> dict:
        return self.x11.screen_info()

    def _cmd_diag(self, message: dict) -> dict:
        """Stack traces of all threads - used to debug stuck commands."""
        import threading
        import traceback

        frames = sys._current_frames()
        out = []
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            stack = traceback.format_stack(frame) if frame else []
            out.append({
                "name": thread.name,
                "state": "alive" if thread.is_alive() else "dead",
                "stack": [line.strip() for line in stack[-6:]],
            })
        return {"threads": out}

    # -- windows ----------------------------------------------------------
    def _cmd_win_list(self, message: dict) -> dict:
        return {"windows": self.x11.list_windows()}

    def _cmd_win_info(self, message: dict) -> dict:
        info = self.x11.window_info(int(message["id"]))
        if not info:
            raise ApiError("window %s not found" % message.get("id"))
        return info

    def _cmd_win_focus(self, message: dict) -> dict:
        wid = int(message["id"])
        ok = self.x11.focus(wid)
        self._active_window = wid
        return {"id": wid, "focused": ok}

    def _cmd_win_resize(self, message: dict) -> dict:
        wid = int(message["id"])
        return self.x11.resize(wid, int(message["w"]), int(message["h"]))

    def _cmd_win_move(self, message: dict) -> dict:
        wid = int(message["id"])
        return self.x11.move(wid, int(message["x"]), int(message["y"]))

    def _cmd_win_close(self, message: dict) -> dict:
        wid = int(message["id"])
        return {"id": wid, "closed": self.x11.close_window(wid)}

    # -- input ------------------------------------------------------------
    def _resolve_point(self, message: dict) -> tuple[int, int]:
        nx = float(message.get("nx", 0.5))
        ny = float(message.get("ny", 0.5))
        win_id = message.get("win")
        if win_id:
            geo = self.x11.geometry(int(win_id), max_age=0.25)
            if not geo:
                raise ApiError("window %s not found" % win_id)
            x, y, w, h = geo
            return int(x + nx * w), int(y + ny * h)
        width, height = self.x11.screen_size()
        return int(nx * width), int(ny * height)

    def _cmd_input_mouse(self, message: dict) -> dict:
        kind = message.get("kind", "move")
        button = int(message.get("btn", 1))
        if kind in ("down", "up", "click"):
            win_id = message.get("win")
            if win_id and self._active_window != int(win_id):
                self.x11.focus(int(win_id))
                self._active_window = int(win_id)
        if kind == "wheel":
            self.x11.wheel(int(message.get("dy", 1)))
            return {"ok": True}
        x, y = self._resolve_point(message)
        if kind == "move":
            self.x11.pointer(x, y)
        elif kind == "down":
            self.x11.pointer(x, y)
            self.x11.button(button, True)
        elif kind == "up":
            self.x11.pointer(x, y)
            self.x11.button(button, False)
        elif kind == "click":
            self.x11.pointer(x, y)
            self.x11.button(button, True)
            self.x11.button(button, False)
        return {"ok": True, "x": x, "y": y}

    def _cmd_input_key(self, message: dict) -> dict:
        combo = message.get("combo") or message.get("key") or ""
        down = bool(message.get("down", True))
        if not combo:
            raise ApiError("no key given")
        self.x11.combo(combo, down)
        return {"ok": True}

    def _cmd_input_text(self, message: dict) -> dict:
        text = message.get("text", "")
        if text:
            self.x11.type_text(text)
        return {"ok": True}

    # -- processes --------------------------------------------------------
    def _cmd_proc_list(self, message: dict) -> dict:
        only_user = bool(message.get("only_user", True))
        app_pids = {
            w["pid"]
            for w in self.x11.list_windows()
            if w["pid"] and w.get("role", "app") == "app"
        }
        rows = self.procs.list(only_user=only_user, app_pids=app_pids)
        return {"processes": rows, "user": self.procs.username}

    def _cmd_proc_kill(self, message: dict) -> dict:
        pid = int(message["pid"])
        ok = self.procs.kill(pid, force=bool(message.get("force", False)))
        return {"pid": pid, "killed": ok}

    # -- power ------------------------------------------------------------
    def _cmd_power(self, message: dict) -> dict:
        action = message.get("action", "")
        if action == "off":
            return {"result": self.power.off()}
        if action == "reboot":
            return {"result": self.power.reboot()}
        raise ApiError("unknown power action %r" % action)

    # -- clipboard --------------------------------------------------------
    def _cmd_clip_get(self, message: dict) -> dict:
        return {"text": self.clip.get()}

    def _cmd_clip_set(self, message: dict) -> dict:
        text = message.get("text", "")
        return {"ok": self.clip.set(text)}

    # -- files ------------------------------------------------------------
    def _cmd_file_list(self, message: dict) -> dict:
        return self.files.list()

    def _cmd_file_put(self, message: dict) -> dict:
        name = message.get("name", "file")
        data = message.get("data", "")
        if message.get("base64", True):
            raw = base64.b64decode(data)
        else:
            raw = data.encode("utf-8")
        return self.files.put(name, raw, message.get("folder", "inbox"))

    def _cmd_file_delete(self, message: dict) -> dict:
        name = message.get("name", "")
        ok = self.files.delete(name, message.get("folder", "inbox"))
        return {"name": name, "deleted": ok}

    def _cmd_file_read(self, message: dict) -> dict:
        name = message.get("name", "")
        path = self.files.get(name, message.get("folder", "inbox"))
        data = path.read_bytes()
        return {
            "name": path.name,
            "folder": message.get("folder", "inbox"),
            "size": len(data),
            "data": base64.b64encode(data).decode("ascii"),
        }

    def _cmd_file_publish(self, message: dict) -> dict:
        path = message.get("path", "")
        return self.files.copy_to_outbox(path)

    # -- misc -------------------------------------------------------------
    def _cmd_video_subscribe(self, message: dict) -> dict:
        target = message.get("target")
        return {"ok": True, "target": target}

    def _cmd_sys_info(self, message: dict) -> dict:
        """System metrics: CPU load, memory, disk, temperatures."""
        import glob as glob_mod

        # --- CPU load (1/5/15 min averages) ---
        load1 = load5 = load15 = 0
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
                load1 = float(parts[0])
                load5 = float(parts[1])
                load15 = float(parts[2])
        except Exception:
            pass

        # --- Memory ---
        mem_total = mem_free = mem_available = mem_buffers = mem_cached = 0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemFree:"):
                        mem_free = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        mem_available = int(line.split()[1]) * 1024
                    elif line.startswith("Buffers:"):
                        mem_buffers = int(line.split()[1]) * 1024
                    elif line.startswith("Cached:"):
                        mem_cached = int(line.split()[1]) * 1024
        except Exception:
            pass
        mem_used = mem_total - mem_available if mem_available else mem_total - mem_free
        mem_total_gb = round(mem_total / (1024**3), 1) if mem_total else 0
        mem_used_gb = round(mem_used / (1024**3), 1)
        mem_pct = round(mem_used / mem_total * 100, 1) if mem_total else 0

        # --- Disk ---
        disks = []
        try:
            st = os.statvfs("/")
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            pct = round(used / total * 100, 1) if total else 0
            disks.append({
                "path": "/",
                "total": total,
                "used": used,
                "free": free,
                "pct": pct,
            })
            # Also check /home if separate mount
            try:
                st_home = os.statvfs("/home")
                total_h = st_home.f_blocks * st_home.f_frsize
                free_h = st_home.f_bavail * st_home.f_frsize
                used_h = total_h - free_h
                pct_h = round(used_h / total_h * 100, 1) if total_h else 0
                if pct_h > 0 or total_h != total:
                    disks.append({
                        "path": "/home",
                        "total": total_h,
                        "used": used_h,
                        "free": free_h,
                        "pct": pct_h,
                    })
            except Exception:
                pass
        except Exception:
            pass

        # --- Temperatures ---
        temps = {}
        try:
            for path in glob_mod.glob("/sys/class/hwmon/hwmon*/temp*_input"):
                try:
                    with open(path) as f:
                        temp_milli = int(f.read().strip())
                except Exception:
                    continue
                # Find the name
                hwmon = os.path.dirname(path)
                name = "unknown"
                try:
                    with open(os.path.join(hwmon, "name")) as f:
                        name = f.read().strip()
                except Exception:
                    pass
                label = ""
                try:
                    with open(os.path.join(hwmon, "temp{}_label".format(
                        os.path.basename(path).replace("temp_", "").replace("_input", ""))
                    )) as f:
                        label = f.read().strip()
                except Exception:
                    pass
                temp_c = round(temp_milli / 1000, 1)
                label = label or name
                temps[label] = temp_c
        except Exception:
            pass

        return {
            "load": {"1": round(load1, 2), "5": round(load5, 2), "15": round(load15, 2)},
            "memory": {
                "total_gb": mem_total_gb,
                "used_gb": mem_used_gb,
                "pct": mem_pct,
            },
            "disks": disks,
            "temps": temps,
            "video_codec": self.capture.codec if self.capture else "mjpeg",
            "screen_size": self.capture.x11.screen_size() if self.capture else None,
        }

    def _cmd_app_launch(self, message: dict) -> dict:
        """Launch an application by name."""
        name = message.get("name", "")
        apps = {
            "firefox": "firefox",
            "terminal": "xfce4-terminal",
            "thunar": "thunar",
            "geany": "geany",
            "vlc": "vlc",
            "libreoffice": "soffice",
        }
        cmd = apps.get(name)
        if not cmd:
            raise ApiError("unknown app %r" % name)
        os.environ.setdefault("DISPLAY", self.x11.name)
        os.system(f"{cmd} &")
        return {"ok": True, "name": name}

    # ---- terminal (PTY) ----
    def _cmd_term_new(self, message: dict) -> dict:
        """Open a new pseudo-terminal session."""
        cols = int(message.get("cols", 120))
        rows = int(message.get("rows", 40))
        pid, fd = pty.fork()  # NOTE: pty.fork() returns (pid, fd), not (fd, pid)
        if pid == 0:  # child
            os.environ["HOME"] = os.path.expanduser("~")
            os.environ.setdefault("DISPLAY", self.x11.name)
            os.environ.setdefault("SHELL", "/bin/bash")
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            os.environ.setdefault("LANG", "en_US.UTF-8")
            os.environ["PS1"] = "\\[\\033[01;32m\\]\\u@\\h\\[\\033[00m\\]:\\[\\033[01;34m\\]\\w\\[\\033[00m\\]\\$ "
            os.execlp("bash", "bash", "--norc", "--noprofile")
        # parent
        with self._term_lock:
            tid = pid
            self._terminals[tid] = {"fd": fd, "pid": pid, "dec": codecs.getincrementaldecoder("utf-8")("replace")}
        # Non-blocking reads: select() caps fds at FD_SETSIZE (1024) and the
        # agent's fds quickly exceed that, so term.read uses os.read + EAGAIN.
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass
        return {"tid": tid, "ok": True}

    def _cmd_term_data(self, message: dict) -> dict:
        """Write data to a terminal."""
        tid = message.get("tid")
        data = message.get("data", "")
        with self._term_lock:
            t = self._terminals.get(tid)
        if not t:
            raise ApiError("terminal %s not found" % tid)
        try:
            os.write(t["fd"], data.encode("utf-8"))
        except OSError as e:
            raise ApiError("write error: %s" % e)
        return {"ok": True}

    def _cmd_term_resize(self, message: dict) -> dict:
        """Resize a terminal."""
        tid = message.get("tid")
        cols = int(message.get("cols", 120))
        rows = int(message.get("rows", 40))
        with self._term_lock:
            t = self._terminals.get(tid)
        if not t:
            raise ApiError("terminal %s not found" % tid)
        try:
            fcntl.ioctl(t["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception as e:
            raise ApiError("resize error: %s" % e)
        return {"ok": True}

    def _cmd_term_read(self, message: dict) -> dict:
        """Read pending output from a terminal (non-blocking)."""
        tid = message.get("tid")
        with self._term_lock:
            t = self._terminals.get(tid)
        if not t:
            return {"data": ""}
        chunks = []
        try:
            while True:
                try:
                    data = os.read(t["fd"], 65536)
                except BlockingIOError:
                    break  # no more output pending
                if not data:
                    break
                chunks.append(data)
                if sum(len(c) for c in chunks) >= 256 * 1024:
                    break
        except OSError:
            return {"data": ""}
        return {"data": t["dec"].decode(b"".join(chunks))}

    def _cmd_term_close(self, message: dict) -> dict:
        """Close a terminal and terminate its shell process."""
        tid = message.get("tid")
        with self._term_lock:
            t = self._terminals.pop(tid, None)
        if t:
            try:
                os.close(t["fd"])
            except OSError:
                pass
            try:
                os.kill(t["pid"], signal.SIGHUP)
                time.sleep(0.1)
                os.kill(t["pid"], signal.SIGKILL)
            except OSError:
                pass
        return {"ok": True}

    def _cmd_shutdown(self, message: dict) -> dict:  # pragma: no cover
        os._exit(0)


__all__ = ["Api", "ApiError", "X11Error"]
