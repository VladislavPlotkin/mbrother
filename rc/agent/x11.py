"""X11 layer of the agent: window inspection/manipulation and input injection.

Two back ends are used on purpose:

* ``python3-xlib`` for *reading* (EWMH window list, titles, classes, pids,
  geometry) and for injecting input through the XTEST extension - it is
  in-process, so a mouse move costs microseconds instead of a fork.
* ``xdotool`` for the few operations that need to talk to the window manager
  (activate / resize / move), because it knows how to go through reparenting
  window managers such as xfwm4.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from Xlib import X, XK, display, error
from Xlib.ext import xtest

# Keys that have to be pressed together with the main key.
MODIFIER_KEYSYMS = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "shift": "Shift_L",
    "super": "Super_L",
    "meta": "Super_L",
}

# Desktop furniture that is not an application window.
PANEL_CLASSES = {
    "xfdesktop",
    "xfce4-panel",
    "xfce4-notificationd",
    "x-window-selector",
    "mate-panel",
    "gnome-shell",
    "plasmashell",
}


class X11Error(Exception):
    pass


def _xdotool(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["xdotool", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise X11Error("xdotool is not installed") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise X11Error("xdotool timed out: %s" % " ".join(args)) from exc


class X11:
    """Thin, thread safe wrapper around the display we want to control.

    The X protocol has no request timeout: if the agent asks a window for a
    property and the owner of that window does not answer, the X server keeps
    the request open forever and the whole connection stalls.  Therefore the
    socket carries a timeout and a broken connection is reopened instead of
    blocking every command.
    """

    def __init__(self, display_name: str | None = None, timeout: float = 5.0):
        self.name = display_name or os.environ.get("DISPLAY", ":0.0")
        self._timeout = timeout
        self._lock = threading.RLock()
        self._geo_cache: dict[int, tuple] = {}
        self._geo_ts = 0.0
        self.dpy = None
        self.root = None
        self._open()

    def _open(self) -> None:
        try:
            self.dpy = display.Display(self.name)
        except Exception as exc:  # pragma: no cover
            raise X11Error("cannot open display %s: %s" % (self.name, exc))
        sock = getattr(getattr(self.dpy, "display", None), "socket", None)
        if sock is not None:
            try:
                sock.settimeout(self._timeout)
            except OSError:  # pragma: no cover
                pass
        self.root = self.dpy.screen().root

    def _reopen(self) -> None:
        """Replace a stalled connection with a fresh one."""
        try:
            self.dpy.close()
        except Exception:  # pragma: no cover
            pass
        self._open()
        self._geo_cache.clear()

    # ------------------------------------------------------------------ misc
    def screen_size(self) -> tuple[int, int]:
        with self._lock:
            try:
                geom = self.root.get_geometry()
            except OSError:
                self._reopen()
                return 0, 0
        return geom.width, geom.height

    def screen_info(self) -> dict:
        width, height = self.screen_size()
        return {"width": width, "height": height}

    def close(self) -> None:
        with self._lock:
            try:
                self.dpy.close()
            except Exception:  # pragma: no cover
                pass

    # -------------------------------------------------------------- reading
    def _prop(self, window, name: str, prop_type=X.AnyPropertyType):
        """Return the *value* of a window property (or None).

        ``get_full_property`` returns the protocol reply; depending on the
        python-xlib version the value sits in ``reply.value`` or in
        ``reply.property.value``.
        """
        with self._lock:
            try:
                atom = self.dpy.intern_atom(name)
                reply = window.get_full_property(atom, prop_type)
            except (error.BadWindow, error.XError):
                return None
            except OSError:
                # The X server kept the request open (the window owner does
                # not answer) - drop the connection and start over.
                self._reopen()
                return None
        if reply is None:
            return None
        value = getattr(reply, "value", None)
        if value is None:
            prop = getattr(reply, "property", None)
            value = getattr(prop, "value", None)
        return value

    def _prop_atom_name(self, window, name: str) -> str:
        value = self._prop(window, name)
        if value is None:
            return ""
        with self._lock:
            try:
                if isinstance(value, (list, tuple)):
                    value = value[0]
                return self.dpy.get_atom_name(int(value))
            except Exception:
                return ""

    @staticmethod
    def _decode(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if isinstance(value, (list, tuple)):
            return "".join(X11._decode(v) for v in value)
        return str(value)

    def window_info(self, wid: int) -> dict | None:
        """Everything the UI needs to know about one top level window."""
        with self._lock:
            try:
                win = self.dpy.create_resource_object("window", wid)
                geom = win.get_geometry()
                # python-xlib: translate_coords(src_window, src_x, src_y)
                # translates a point of the SOURCE window into the coordinates
                # of self - so the root has to be the destination.
                abs_pos = self.root.translate_coords(win, 0, 0)
            except (error.BadWindow, error.XError):
                return None
            except OSError:
                self._reopen()
                return None

            title = self._decode(self._prop(win, "_NET_WM_NAME"))
            if not title:
                title = self._decode(self._prop(win, "WM_NAME"))
            wm_class = self._decode(self._prop(win, "WM_CLASS")).split("\x00")
            wm_class = [c for c in wm_class if c]
            pid_prop = self._prop(win, "_NET_WM_PID")
            pid = int(pid_prop[0]) if pid_prop else 0
            state = self._prop_atom_name(win, "WM_STATE")
            role = "app"
            if wm_class and wm_class[0].lower() in PANEL_CLASSES:
                role = "panel"

            return {
                "id": int(wid),
                "title": title or "(без названия)",
                "class": wm_class[0] if wm_class else "",
                "instance": wm_class[1] if len(wm_class) > 1 else "",
                "pid": pid,
                "x": int(abs_pos.x),
                "y": int(abs_pos.y),
                "w": int(geom.width),
                "h": int(geom.height),
                "state": state,
                "role": role,
                "minimized": state == "IconicState",
            }

    def list_windows(self) -> list[dict]:
        """Top level windows known to the window manager (EWMH client list)."""
        with self._lock:
            prop = self._prop(self.root, "_NET_CLIENT_LIST")
            ids = [int(i) for i in prop] if prop else []
        windows = []
        for wid in ids:
            info = self.window_info(wid)
            if info:
                windows.append(info)
        windows.sort(key=lambda w: (w["role"] != "app", w["state"] == "IconicState", w["title"].lower()))
        return windows

    def geometry(self, wid: int, max_age: float = 0.2) -> tuple[int, int, int, int] | None:
        """Absolute geometry (x, y, w, h) of a window, cached briefly."""
        now = time.monotonic()
        with self._lock:
            cached = self._geo_cache.get(wid)
            if cached and now - self._geo_ts < max_age:
                return cached
        info = self.window_info(wid)
        if not info:
            return None
        geo = (info["x"], info["y"], info["w"], info["h"])
        with self._lock:
            self._geo_cache[wid] = geo
            self._geo_ts = now
        return geo

    def invalidate_geometry(self, wid: int | None = None) -> None:
        with self._lock:
            if wid is None:
                self._geo_cache.clear()
            else:
                self._geo_cache.pop(wid, None)

    # -------------------------------------------------------------- actions
    def focus(self, wid: int) -> bool:
        proc = _xdotool("windowactivate", "--sync", str(wid))
        if proc.returncode != 0:
            with self._lock:
                try:
                    win = self.dpy.create_resource_object("window", wid)
                    win.set_input_focus(X.RevertToParent, X.CurrentTime)
                    self.dpy.flush()
                except (error.BadWindow, error.XError):
                    return False
        self.invalidate_geometry(wid)
        return True

    def resize(self, wid: int, width: int, height: int) -> dict:
        width = max(1, int(width))
        height = max(1, int(height))
        proc = _xdotool("windowsize", str(wid), str(width), str(height))
        if proc.returncode != 0:
            raise X11Error(proc.stderr.strip() or "windowsize failed")
        self.invalidate_geometry(wid)
        time.sleep(0.15)
        geo = self.geometry(wid, max_age=0.0)
        return {"id": wid, "w": width, "h": height, "geometry": geo}

    def move(self, wid: int, x: int, y: int) -> dict:
        proc = _xdotool("windowmove", str(wid), str(int(x)), str(int(y)))
        if proc.returncode != 0:
            raise X11Error(proc.stderr.strip() or "windowmove failed")
        self.invalidate_geometry(wid)
        time.sleep(0.15)
        geo = self.geometry(wid, max_age=0.0)
        return {"id": wid, "x": int(x), "y": int(y), "geometry": geo}

    def close_window(self, wid: int) -> bool:
        """Ask the window to close politely (WM_DELETE_WINDOW)."""
        with self._lock:
            try:
                win = self.dpy.create_resource_object("window", wid)
                wm_protocols = self.dpy.intern_atom("WM_PROTOCOLS")
                wm_delete = self.dpy.intern_atom("WM_DELETE_WINDOW")
                event = Xlib_event_client_message(
                    self.dpy,
                    win,
                    wm_protocols,
                    [wm_delete, X.CurrentTime, 0, 0, 0],
                )
                # The event has to be delivered to the client window itself:
                # sending it to the root would hand it to the window manager.
                win.send_event(event, event_mask=X.NoEventMask)
                self.dpy.flush()
                return True
            except (error.BadWindow, error.XError):
                return False

    # ---------------------------------------------------------------- input
    def _keycode(self, keysym_name: str) -> int:
        keysym = XK.string_to_keysym(keysym_name)
        if not keysym and len(keysym_name) == 1:
            # A single character that has no keysym name ("-"): Latin-1 keeps
            # its code point, everything else goes to the Unicode area.
            code_point = ord(keysym_name)
            keysym = code_point if code_point < 0x100 else 0x01000000 + code_point
        if not keysym:
            raise X11Error("no keysym for %r" % keysym_name)
        with self._lock:
            code = self.dpy.keysym_to_keycode(keysym)
        if not code:
            raise X11Error("no keycode for %r" % keysym_name)
        return code

    def pointer(self, x: int, y: int) -> None:
        with self._lock:
            xtest.fake_input(self.dpy, X.MotionNotify, x=int(x), y=int(y))
            self.dpy.flush()

    def button(self, button: int, pressed: bool) -> None:
        with self._lock:
            xtest.fake_input(
                self.dpy,
                X.ButtonPress if pressed else X.ButtonRelease,
                int(button),
            )
            self.dpy.flush()

    def wheel(self, steps: int) -> None:
        button = 4 if steps > 0 else 5
        for _ in range(max(1, abs(steps))):
            with self._lock:
                xtest.fake_input(self.dpy, X.ButtonPress, button)
                xtest.fake_input(self.dpy, X.ButtonRelease, button)
                self.dpy.flush()
            time.sleep(0.01)

    def key(self, keysym_name: str, pressed: bool) -> None:
        code = self._keycode(keysym_name)
        with self._lock:
            xtest.fake_input(self.dpy, X.KeyPress if pressed else X.KeyRelease, code)
            self.dpy.flush()

    def combo(self, combo: str, pressed: bool) -> None:
        """Press/release a combination such as ``ctrl+shift+t``."""
        parts = [p for p in combo.lower().split("+") if p]
        if not parts:
            return
        keys = parts[:-1]
        main = parts[-1]
        mod_codes = [self._keycode(MODIFIER_KEYSYMS[k]) for k in keys if k in MODIFIER_KEYSYMS]
        main_code = self._keycode(main)
        with self._lock:
            if pressed:
                for code in mod_codes:
                    xtest.fake_input(self.dpy, X.KeyPress, code)
                xtest.fake_input(self.dpy, X.KeyPress, main_code)
            else:
                xtest.fake_input(self.dpy, X.KeyRelease, main_code)
                for code in reversed(mod_codes):
                    xtest.fake_input(self.dpy, X.KeyRelease, code)
            self.dpy.flush()

    def _keysym_keycode(self, keysym: int) -> tuple[int, bool]:
        """Map a keysym to a keycode and tell whether Shift has to be held.

        Characters like ``_`` or ``B`` live on the shifted level of a key, so
        the plain keycode would produce ``-`` and ``b``.
        """
        with self._lock:
            code = self.dpy.keysym_to_keycode(keysym)
            if not code:
                return 0, False
            try:
                mapping = self.dpy.get_keyboard_mapping(code, 1)[0]
            except Exception:
                return code, False
        if len(mapping) > 1 and mapping[1] == keysym and mapping[0] != keysym:
            return code, True
        return code, False

    def type_text(self, text: str, delay: float = 0.012) -> None:
        """Type literal text (used for the clipboard -> window bridge)."""
        shift_code = 0
        for ch in text:
            keysym = XK.string_to_keysym(ch)
            if not keysym:
                # Latin-1 characters keep their code point as keysym
                # ("-" -> 0x2d "minus"), everything above lives in the
                # "Unicode keysym" area starting at 0x01000000.
                code_point = ord(ch)
                keysym = code_point if code_point < 0x100 else 0x01000000 + code_point
            code, need_shift = self._keysym_keycode(keysym)
            if not code:
                continue
            if need_shift and not shift_code:
                shift_code = self._keycode("Shift_L")
            with self._lock:
                if need_shift:
                    xtest.fake_input(self.dpy, X.KeyPress, shift_code)
                xtest.fake_input(self.dpy, X.KeyPress, code)
                xtest.fake_input(self.dpy, X.KeyRelease, code)
                if need_shift:
                    xtest.fake_input(self.dpy, X.KeyRelease, shift_code)
                self.dpy.flush()
            time.sleep(delay)

    def list_keyboards(self) -> list[str]:
        proc = _xdotool("getactivewindow")
        del proc
        return ["XTEST"]


def Xlib_event_client_message(dpy, window, msg_type, data):
    """Build a ClientMessage event (kept separate to avoid an import cycle)."""
    from Xlib import protocol

    return protocol.event.ClientMessage(
        window=window,
        client_type=msg_type,
        data=(32, data),
    )
