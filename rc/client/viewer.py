"""Native floating viewer: the remote screen in its own window.

Video comes straight from the agent over a single TCP connection and is
drawn on a Tk canvas; input goes over the shared request link.  There is no
HTTP hop and no browser in between, so the picture is as fresh as the agent
can make it.

    rc viewer [--source screen|win:ID] [--fullscreen]
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
try:
    import tkinter as tk
    HAS_TK = True
except ImportError:
    HAS_TK = False

from PIL import Image, ImageTk

from ..protocol import recv_raw
from .agent_link import AgentLink, open_video_stream
from .h264_decoder import H264StreamDecoder  # noqa: F401 — нужен для импорта
from .h265_decoder import H265StreamDecoder  # noqa: F401 — нужен для импорта

SOI = b"\xff\xd8"  # начала JPEG — по нему распознаём MJPEG-кадры

BG = "#0b0d11"
BAR = "#171b22"
FG = "#d7dce3"
MUTED = "#8b95a3"
LINE = "#2a313c"
ACCENT = "#1d4b7a"
LOCK_BG = "#7a3e10"    # фон кнопки, когда режим блокировки включён
LOCK_FG = "#ffd9a0"

# Ограничения авто-подгонки окна под картинку: при маленьком масштабе окно
# не должно сжиматься до состояния, когда ползунок уже не доступен.
MIN_WIN_W = 300   # «Источник» + ползунок масштаба
MIN_WIN_H = 60    # минимальная высота области картинки


def _get_monitors() -> list[tuple[int, int, int, int]]:
    """Возвращает список кортежей (x, y, w, h) для каждого подключённого монитора."""
    try:
        out = subprocess.check_output(
            ["xrandr", "--query"], stderr=subprocess.DEVNULL, text=True, timeout=3
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return [(0, 0, 1920, 1080)]  # fallback на один монитор

    monitors: list[tuple[int, int, int, int]] = []
    for line in out.splitlines():
        m = re.search(
            r"(\S+) connected\s+(\d+)x(\d+)\+(\d+)\+(\d+)", line
        )
        if m:
            w, h, x, y = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            monitors.append((x, y, w, h))
    return monitors if monitors else [(0, 0, 1920, 1080)]


def _cursor_position() -> tuple[int, int]:
    """Возвращает (x, y) позиции курсора мыши относительно начала виртуального экрана."""
    try:
        out = subprocess.check_output(
            ["xdotool", "getmouselocation"], stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        # формат: "x:123 y:456 screen:0 window:0x123"
        mx = my = 0
        for part in out.split():
            if part.startswith("x:"):
                mx = int(part[2:])
            elif part.startswith("y:"):
                my = int(part[2:])
        return (mx, my)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return (0, 0)


def _window_pos_on_cursor_monitor(w: int, h: int) -> tuple[int, int]:
    """Вычисляет (x, y) для размещения окна с центром на мониторе, где находится курсор."""
    monitors = _get_monitors()
    cx, cy = _cursor_position()

    best = monitors[0]  # fallback
    for mon in monitors:
        mx, my, mw, mh = mon
        if mx <= cx < mx + mw and my <= cy < my + mh:
            best = mon
            break

    bx, by, bw, bh = best
    # Центрируем окно на этом мониторе (но не выходим за границы)
    ox = bx + max(0, min((bw - w) // 2, bw - w))
    oy = by + max(0, min((bh - h) // 2, bh - h))
    return (ox, oy)

# Tk reports the character that was produced, not the key that was pressed,
# so shifted punctuation has to be translated back into key + shift.
SHIFTED = {
    "question": "shift+slash", "exclam": "shift+1", "at": "shift+2",
    "numbersign": "shift+3", "dollar": "shift+4", "percent": "shift+5",
    "asciicircum": "shift+6", "ampersand": "shift+7", "asterisk": "shift+8",
    "parenleft": "shift+9", "parenright": "shift+0", "underscore": "shift+minus",
    "plus": "shift+equal", "braceleft": "shift+bracketleft",
    "braceright": "shift+bracketright", "bar": "shift+backslash",
    "colon": "shift+semicolon", "quotedbl": "shift+apostrophe",
    "less": "shift+comma", "greater": "shift+period",
    "asciitilde": "shift+grave",
}

NAMED = {
    "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Insert",
    "Delete", "BackSpace", "Return", "Tab", "Escape", "space", "Pause",
    "Print", "Menu", "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L", "Alt_R", "Super_L", "Super_R", "Caps_Lock", "Num_Lock",
}
for _n in range(1, 13):
    NAMED.add("F%d" % _n)


if HAS_TK:
    class Viewer:
        def __init__(self, host: str, port: int = 7777, source: str = "screen",
                     fullscreen: bool = False):
            self.host = host
            self.port = port
            self.target = None
            self.source = source
            self.scale_factor = 1.0        # масштаб картинки: ползунок 10..150%
            self._slider_dragging = False  # пользователь тянет ползунок
            self._slider_after = None      # отложенная подгонка окна
            self._pre_fullscreen_pct = 100 # масштаб до входа в полный экран

            # Состояния отображения
            self._toolbar_visible = True   # True = панель видима
            self._locked = False           # True = блокировка мыши (ввод не уходит агенту)
            self.shown = (0, 0, 0, 0)
            self.photo = None
            self.frames = 0
            self.fps = 0.0
            self.ping_ms = None
            self.status = "подключение…"
            self._frame = None
            self._frame_lock = threading.Lock()
            self._count_lock = threading.Lock()
            self._live = None
            self._vsock = None
            self._closing = False
            self._last_stats = time.time()
            self._video_dims: tuple[int, int] | None = None
    
            self.link = AgentLink(host, port)
            if source.startswith("win:"):
                try:
                    self.target = {"win": int(source[4:])}
                except ValueError:
                    self.target = None
    
            self._build_ui(fullscreen)
            if not self.link.connect():
                self.status = "агент недоступен: %s" % (self.link.last_error or "нет связи")
            self._start_video()
            threading.Thread(target=self._ping_loop, daemon=True).start()
    
        # ------------------------------------------------------------------- ui
        def _build_ui(self, fullscreen: bool) -> None:
            self.root = tk.Tk()
            self.root.title("rc — просмотр")
            # Размещаем окно на том мониторе, где сейчас курсор
            win_w, win_h = 1100, 700
            ox, oy = _window_pos_on_cursor_monitor(win_w, win_h)
            self.root.geometry(f"{win_w}x{win_h}+{ox}+{oy}")
            self.root.configure(bg=BG)
            self.root.protocol("WM_DELETE_WINDOW", self.close)
    
            self.toolbar = tk.Frame(self.root, bg=BAR)
            self.toolbar.pack(side="top", fill="x")
    
            self.src_button = tk.Menubutton(
                self.toolbar, text="Источник", bg=BAR, fg=FG, activebackground=ACCENT,
                activeforeground=FG, relief="flat", padx=10, pady=4, highlightthickness=0,
            )
            self.menu = tk.Menu(self.src_button, tearoff=0, bg=BAR, fg=FG,
                                activebackground=ACCENT, activeforeground=FG)
            self.menu.configure(postcommand=self._fill_sources)
            self.src_button["menu"] = self.menu
            self.src_button.pack(side="left", padx=(8, 4), pady=5)
    
            # Ползунок масштаба: 10..150% с шагом 1%
            self.scale_text = tk.StringVar(value="100%")
            self.scale_slider = tk.Scale(
                self.toolbar, from_=10, to=150, orient="horizontal", resolution=1,
                showvalue=0, length=120, bg=MUTED, fg=FG, troughcolor=LINE,
                activebackground=ACCENT, highlightthickness=0, sliderrelief="flat",
                borderwidth=0, command=self._on_slider,
            )
            self.scale_slider.set(100)
            self.scale_slider.bind("<ButtonPress-1>", self._on_slider_press)
            self.scale_slider.bind("<ButtonRelease-1>", self._on_slider_release)
            self.scale_slider.pack(side="left", padx=(2, 0), pady=5)
            tk.Label(
                self.toolbar, textvariable=self.scale_text, bg=BAR, fg=FG, padx=4,
            ).pack(side="left", pady=5)
            # Кнопка сброса на 100%
            tk.Button(
                self.toolbar, text="100%", bg=BAR, fg=FG, activebackground=ACCENT,
                activeforeground=FG, relief="flat", padx=6, pady=4, highlightthickness=0,
                font=("", 9, "bold"), command=self._reset_scale_100,
            ).pack(side="left", padx=2, pady=5)
    
            self.fs_btn = tk.Button(
                self.toolbar, text="Во весь экран", bg=BAR, fg=FG, activebackground=ACCENT,
                activeforeground=FG, relief="flat", padx=8, pady=4, highlightthickness=0,
                command=self._toggle_fullscreen,
            )
            self.fs_btn.pack(side="left", padx=(8, 2), pady=5)
            # Кнопка «Без рамки» / «С рамкой»
            self.toolbar_btn = tk.Button(
                self.toolbar, text="Скрыть панель", bg=BAR, fg=FG, activebackground=ACCENT,
                activeforeground=FG, relief="flat", padx=8, pady=4, highlightthickness=0,
                command=self._toggle_toolbar,
            )
            self.toolbar_btn.pack(side="left", padx=2, pady=5)

            # Drag за пустое место панели
            self.toolbar.bind("<ButtonPress-1>", self._drag_start)
            self.toolbar.bind("<B1-Motion>", self._drag_move)
            tk.Button(
                self.toolbar, text="Переподключить", bg=BAR, fg=FG, activebackground=ACCENT,
                activeforeground=FG, relief="flat", padx=8, pady=4, highlightthickness=0,
                command=self._start_video,
            ).pack(side="left", padx=2, pady=5)
            # Режим блокировки: пока включён — движения/клики мыши не отправляются
            self.lock_btn = tk.Button(
                self.toolbar, text="Блокировка", bg=BAR, fg=FG, activebackground=ACCENT,
                activeforeground=FG, relief="flat", padx=8, pady=4, highlightthickness=0,
                command=self._toggle_lock,
            )
            self.lock_btn.pack(side="left", padx=2, pady=5)

            self.stats = tk.Label(self.toolbar, text="", bg=BAR, fg=MUTED, padx=10)
            self.stats.pack(side="right", pady=5)
            self.stats.bind("<ButtonPress-1>", self._drag_start)
            self.stats.bind("<B1-Motion>", self._drag_move)
    
            self.canvas = tk.Canvas(self.root, bg="#000000", highlightthickness=0, bd=1, relief="solid",
                                    cursor="crosshair")
            self.canvas.pack(side="top", fill="both", expand=True)
    
            self.item = self.canvas.create_image(0, 0, anchor="nw")

            # Оверлей «БЛОКИРОВКА» поверх видео (показывается при включённом режиме)
            self._lock_overlay = self.canvas.create_text(
                0, 0, anchor="ne", text="  БЛОКИРОВКА  ", fill=LOCK_FG,
                font=("", 12, "bold"), state="hidden",
            )

            self.canvas.bind("<Motion>", self._on_motion)
            self.canvas.bind("<ButtonPress-1>", lambda e: self._on_button(e, True, 1))
            self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_button(e, False, 1))
            self.canvas.bind("<ButtonPress-2>", lambda e: self._on_button(e, True, 2))
            self.canvas.bind("<ButtonRelease-2>", lambda e: self._on_button(e, False, 2))
            self.canvas.bind("<ButtonPress-3>", lambda e: self._on_button(e, True, 3))
            self.canvas.bind("<ButtonRelease-3>", lambda e: self._on_button(e, False, 3))
            self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 1))
            self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))
            self.canvas.bind("<Configure>", lambda e: self._relayout())
            self.root.bind("<KeyPress>", self._on_keydown)
            self.root.bind("<KeyRelease>", self._on_keyup)
            self.canvas.focus_set()
    
            if fullscreen:
                self.root.after(200, self._toggle_fullscreen)
    
        # --------------------------------------------------------------- масштаб
        def _on_slider(self, value: str) -> None:
            """Scale -command: новый масштаб в процентах (10..150)."""
            try:
                pct = int(round(float(value)))
            except ValueError:
                return
            self.scale_factor = max(10, min(150, pct)) / 100.0
            self.scale_text.set("%d%%" % int(round(self.scale_factor * 100)))
            if getattr(self, "item", None) is None:
                return  # идёт построение UI — картинки ещё нет
            self._relayout()   # сразу показываем новый размер картинки
            if self._slider_dragging:
                return  # пока тянем ползунок — окно не трогаем
            # клавиатура/программная установка: подгоняем окно с задержкой,
            # чтобы не дёргать его на каждом шаге
            if self._slider_after is not None:
                try:
                    self.root.after_cancel(self._slider_after)
                except tk.TclError:
                    pass
            self._slider_after = self.root.after(300, self._fit_window_to_video)

        def _on_slider_press(self, _event=None) -> None:
            self._slider_dragging = True

        def _on_slider_release(self, _event=None) -> None:
            self._slider_dragging = False
            if getattr(self, "item", None) is not None:
                self.root.after_idle(self._fit_window_to_video)

        def _reset_scale_100(self) -> None:
            """Кнопка сброса масштаба на 100%."""
            self._set_scale_percent(100, refit=True)

        def _set_scale_percent(self, pct: int, refit: bool = True) -> None:
            """Программная установка масштаба (полноэкранный режим и тесты)."""
            pct = max(10, min(150, int(pct)))
            self.scale_factor = pct / 100.0
            self.scale_text.set("%d%%" % pct)
            try:
                self.scale_slider.set(pct)   # может вызвать _on_slider — это безопасно
            except tk.TclError:
                pass
            if getattr(self, "item", None) is not None:
                self._relayout()
                if refit:
                    self.root.after_idle(self._fit_window_to_video)
    
        def _relayout(self) -> None:
            self.shown = (0, 0, 0, 0)
            if self._locked:
                self._update_lock_overlay()   # переставить ярлык по новой ширине
            with self._frame_lock:
                frame, self._frame = self._frame, None
            if frame is not None:
                self._show(frame)
    
        def _toggle_fullscreen(self) -> None:
            full = bool(self.root.attributes("-fullscreen"))
            if not full:
                # запоминаем масштаб, чтобы вернуть его после выхода
                self._pre_fullscreen_pct = int(round(self.scale_factor * 100))
                self.root.attributes("-fullscreen", True)
                self.root.after(200, self._fit_fullscreen)
            else:
                self.root.attributes("-fullscreen", False)
                self.root.after(80, self._after_fullscreen)

        def _fit_fullscreen(self) -> None:
            """В полном экране картинка вписывается в экран, ползунок следует."""
            if self._closing or not getattr(self, "item", None):
                return
            try:
                if not self.root.attributes("-fullscreen"):
                    return
            except tk.TclError:
                return
            if not self._video_dims:   # первый кадр ещё не пришёл — ждём
                self.root.after(300, self._fit_fullscreen)
                return
            iw, ih = self._video_dims
            try:
                cw = max(1, self.canvas.winfo_width())
                ch = max(1, self.canvas.winfo_height())
            except tk.TclError:
                return
            pct = int(min(cw / iw, ch / ih) * 100)
            self._set_scale_percent(pct, refit=False)
            self._relayout()

        def _after_fullscreen(self) -> None:
            """После выхода из полного экрана — прежний масштаб и подгонка окна."""
            if getattr(self, "item", None) is None:
                return
            self._set_scale_percent(self._pre_fullscreen_pct, refit=False)
            self._relayout()
            self._fit_window_to_video()

        # --------------------------------------------------- панель

        def _set_toolbar(self, show: bool) -> None:
            """Показать/скрыть панель и обновить кнопку."""
            self._toolbar_visible = show
            if show:
                if not self.toolbar.winfo_manager():
                    self.toolbar.pack(side="top", fill="x", before=self.canvas)
                self.toolbar_btn.configure(text="Скрыть панель")
            else:
                if self.toolbar.winfo_manager():
                    self.toolbar.pack_forget()
                self.toolbar_btn.configure(text="Показать панель")
            self.root.after(50, self._relayout)
            # После скрытия панели окно должно сузиться (canvas не должен
            # заполнять всё освободившееся место)
            if not show:
                self.root.after(150, self._shrink_window)
            self.root.after(200, self._fit_window_to_video)

        def _shrink_window(self) -> None:
            """Сузить окно до размера контента без панели."""
            dims = self._video_dims
            if not dims or self._closing:
                return
            try:
                if self.root.attributes("-fullscreen"):
                    return
            except tk.TclError:
                return
            w, h = dims
            factor = self.scale_factor
            tw = max(1, int(round(w * factor)))
            th = max(1, int(round(h * factor)))
            try:
                self.root.update_idletasks()
                root_w = self.root.winfo_width()
                root_h = self.root.winfo_height()
                canvas_w = self.canvas.winfo_width()
                canvas_h = self.canvas.winfo_height()
            except tk.TclError:
                return
            # Без панели: window = content + (root - canvas)
            new_w = max(tw, MIN_WIN_W) + max(0, root_w - canvas_w)
            new_h = max(th, MIN_WIN_H) + max(0, root_h - canvas_h)
            geom = self.root.geometry()
            try:
                cur_w, cur_h = (int(v) for v in geom.split("+")[0].split("x"))
            except ValueError:
                return
            if abs(cur_w - new_w) <= 2 and abs(cur_h - new_h) <= 2:
                return
            self.root.geometry("%dx%d" % (new_w, new_h))

        def _toggle_toolbar(self) -> None:
            """Переключить панель: видима → скрыть, скрыта → показать."""
            if self.toolbar.winfo_manager():
                self._set_toolbar(show=False)
            else:
                self._set_toolbar(show=True)

        def _toggle_toolbar_manual(self) -> None:
            """Принудительное скрытие панели (кнопка + Esc когда не fullscreen)."""
            if self.toolbar.winfo_manager():
                self._set_toolbar(show=False)

        # ------------------------------------------------------- блокировка мыши
        def _toggle_lock(self) -> None:
            """Включить/выключить режим блокировки: мышь не отправляет ввод агенту."""
            self._locked = not self._locked
            if self._locked:
                self.lock_btn.configure(text="Блокировка ✓", bg=LOCK_BG, fg=LOCK_FG,
                                        activebackground=LOCK_BG, activeforeground=LOCK_FG)
                self.canvas.configure(cursor="X_cursor")   # ввод заблокирован
            else:
                self.lock_btn.configure(text="Блокировка", bg=BAR, fg=FG,
                                        activebackground=ACCENT, activeforeground=FG)
                self.canvas.configure(cursor="crosshair")
            self._update_lock_overlay()

        def _update_lock_overlay(self) -> None:
            """Показать/скрыть надпись «БЛОКИРОВКА» поверх картинки."""
            if not getattr(self, "_lock_overlay", None):
                return
            try:
                if self._locked:
                    self.canvas.itemconfigure(self._lock_overlay, state="normal")
                    # ярлык в правом верхнем углу канваса
                    bw = max(1, self.canvas.winfo_width())
                    self.canvas.coords(self._lock_overlay, bw - 8, 8)
                    self.canvas.tag_raise(self._lock_overlay)
                else:
                    self.canvas.itemconfigure(self._lock_overlay, state="hidden")
            except tk.TclError:
                pass

        def _drag_start(self, event) -> None:
            """Начало перетаскивания окна."""
            event.widget.bind("<ButtonRelease-1>", self._drag_stop)
            self._drag_start_x = event.x_root
            self._drag_start_y = event.y_root
            self._drag_win_x = self.root.winfo_x()
            self._drag_win_y = self.root.winfo_y()

        def _drag_stop(self, event) -> None:
            event.widget.unbind("<ButtonRelease-1>")
            # Клик по панели (без движения) — ничего

        def _drag_move(self, event) -> None:
            self.root.geometry("+%d+%d" % (
                self._drag_win_x + event.x_root - self._drag_start_x,
                self._drag_win_y + event.y_root - self._drag_start_y,
            ))

        # --------------------------------------------------------------- sources
        def _fill_sources(self) -> None:
            self.menu.delete(0, "end")
            self.menu.add_command(label="Весь экран", command=lambda: self._pick(None))
            try:
                data = self.link.call("win.list", timeout=3)
            except Exception:
                return
            for win in data.get("windows", []):
                title = (win.get("title") or "без названия")[:60]
                wid = int(win["id"])
                label = "%s  [%d×%d]" % (title, win.get("w", 0), win.get("h", 0))
                self.menu.add_command(label=label, command=lambda i=wid: self._pick(i))
    
        def _pick(self, wid) -> None:
            self.target = {"win": wid} if wid else None
            self.src_button.configure(text="Окно" if wid else "Источник")
            self._start_video()
    
        # ----------------------------------------------------------------- video
        def _start_video(self) -> None:
            live, sock = self._live, self._vsock
            if live is not None:
                live.clear()
            if sock is not None:
                try:
                    sock.shutdown(0)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            self._vsock = None
            live = threading.Event()
            live.set()
            self._live = live
            threading.Thread(target=self._video_loop, args=(self.target, live),
                             daemon=True).start()
    
        def _detect_codec(self, payload: bytes) -> str:
            """Detect codec from raw frame payload."""
            if len(payload) < 5:
                return "mjpeg"
            # Check for Annex-B start code
            if payload[:4] == b"\x00\x00\x00\x01" or (len(payload) > 3 and payload[:3] == b"\x00\x00\x01"):
                start = 4 if payload[:4] == b"\x00\x00\x00\x01" else 3
                nal_type = payload[start]
                # H.265: nal_type 0x40-0x45 for VPS/SPS/PPS (first byte of NAL)
                if 0x40 <= nal_type <= 0x45:
                    return "h265"
                # For nal_type < 0x40, check decoded type
                # H.265 VCL NAL: decoded type 0-31, non-VCL: 32-34
                h265_decoded = (nal_type & 0x7E) >> 1
                if h265_decoded <= 34:
                    return "h265"
                # H.264: SPS=0x67, PPS=0x68
                h264_decoded = nal_type & 0x1F
                if h264_decoded in (7, 8):
                    return "h264"
            # Check JPEG signature
            if payload[:2] == SOI:
                return "mjpeg"
            return "mjpeg"  # default

        def _video_loop(self, target, live) -> None:
            decoder = None
            decoder_type = None  # 'h264' or 'h265'
            while live.is_set() and not self._closing:
                sock = None
                try:
                    sock = open_video_stream(self.host, self.port, target)
                    self._vsock = sock
                    while live.is_set():
                        mtype, payload = recv_raw(sock)
                        if mtype != 0x02:
                            continue
                        if payload[:2] == SOI:
                            try:
                                image = Image.open(io.BytesIO(payload))
                                image.load()
                            except Exception:
                                continue
                        else:
                            # Detect codec on first non-JPEG frame
                            if decoder_type is None:
                                codec = self._detect_codec(payload)
                                if codec == "h265":
                                    decoder_type = "h265"
                                elif codec == "h264":
                                    decoder_type = "h264"
                                else:
                                    # Assume MJPEG, skip this frame
                                    continue
                            # Create or reuse decoder based on detected codec
                            if decoder is None:
                                try:
                                    if decoder_type == "h265":
                                        decoder = H265StreamDecoder()
                                    else:
                                        decoder = H264StreamDecoder()
                                except Exception as exc:
                                    self.status = "нет PyAV: H.%s недоступен: %s" % (
                                        "265" if decoder_type == "h265" else "264", exc
                                    )
                                    time.sleep(1.0)
                                    continue
                            image = decoder.feed(payload)
                            if image is None:
                                continue
                        with self._frame_lock:
                            self._frame = image
                        with self._count_lock:
                            self.frames += 1
                except Exception as exc:
                    if not live.is_set():
                        break
                    self.status = "нет связи: %s" % exc
                    time.sleep(1.0)
                finally:
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                if not live.is_set():
                    break
        
        def _fit_window_to_video(self) -> None:
            """Подогнать размер окна под видео при текущем масштабе ползунка.

            Вызывается при смене разрешения удалённого экрана, при отпускании
            ползунка, при возврате из полного экрана и при переключении панели.
            Размер рамки окна измеряется, а не задаётся константой, поэтому
            панель инструментов и рамки WM учитываются автоматически.  Окно не
            сжимается ниже MIN_WIN_W×MIN_WIN_H — иначе ползунком было бы не
            управляться.
            """
            dims = self._video_dims
            if not dims or self._closing:
                return
            try:
                if self.root.attributes("-fullscreen"):
                    return
            except tk.TclError:
                return
            w, h = dims
            factor = self.scale_factor
            tw = max(1, int(round(w * factor)))
            th = max(1, int(round(h * factor)))
            try:
                self.root.update_idletasks()
                root_w = self.root.winfo_width()
                root_h = self.root.winfo_height()
                canvas_w = self.canvas.winfo_width()
                canvas_h = self.canvas.winfo_height()
                toolbar_h = self.toolbar.winfo_height() if self.toolbar.winfo_ismapped() else 0
            except tk.TclError:
                return
            new_w = max(tw, MIN_WIN_W) + max(0, root_w - canvas_w)
            new_h = toolbar_h + max(th, MIN_WIN_H) + max(0, root_h - toolbar_h - canvas_h)
            geom = self.root.geometry()          # "WxH+x+y"
            try:
                cur_w, cur_h = (int(v) for v in geom.split("+")[0].split("x"))
            except ValueError:
                return
            if abs(cur_w - new_w) <= 2 and abs(cur_h - new_h) <= 2:
                return                            # уже в размер — петли нет
            self.root.geometry("%dx%d" % (new_w, new_h))  # позиция сохраняется
        
        def _ping_loop(self) -> None:
            while not self._closing:
                started = time.monotonic()
                try:
                    self.link.call("ping", timeout=3)
                    self.ping_ms = (time.monotonic() - started) * 1000
                    if self.status.startswith("нет связи"):
                        self.status = ""
                except Exception:
                    self.ping_ms = None
                time.sleep(3.0)
    
        # ---------------------------------------------------------------- render
        def _tick(self) -> None:
            started = time.time()
            with self._frame_lock:
                frame, self._frame = self._frame, None
            if frame is not None:
                self.status = ""
                self._show(frame)
            # fps считаем раз в секунду; сбрасывать счётчик кадров можно
            # только в этот момент, иначе fps всегда ~0.
            span = started - self._last_stats
            if span >= 1.0:
                with self._count_lock:
                    frames, self.frames = self.frames, 0
                self.fps = frames / span
                self._last_stats = started
                self._update_stats()
            if not self._closing:
                self.root.after(8, self._tick)
    
        def _update_stats(self) -> None:
            parts = []
            if self.shown[2]:
                parts.append("%d×%d" % (self.shown[2], self.shown[3]))
            parts.append("%.1f fps" % self.fps)
            if self.ping_ms is not None:
                parts.append("пинг %.0f мс" % self.ping_ms)
            if self.status:
                parts.append(self.status)
            self.stats.configure(text=" · ".join(parts), fg=("#e0a050" if self.status else MUTED))
    
        def _display_size(self, iw: int, ih: int) -> tuple[int, int]:
            """Размер картинки на экране = исходный × масштаб ползунка."""
            f = self.scale_factor
            return max(1, int(round(iw * f))), max(1, int(round(ih * f)))
    
        def _show(self, image: Image.Image) -> None:
            # Следим за разрешением источника: при его смене окно должно
            # масштабироваться вслед за видео.
            dims_changed = self._video_dims != image.size
            if dims_changed:
                self._video_dims = image.size
            width, height = self._display_size(image.width, image.height)
            if (width, height) != image.size:
                image = image.resize((width, height), Image.NEAREST)
            self.photo = ImageTk.PhotoImage(image)
            self.canvas.itemconfigure(self.item, image=self.photo)
            cw = max(width, self.canvas.winfo_width())
            ch = max(height, self.canvas.winfo_height())
            x = (cw - width) // 2
            y = (ch - height) // 2
            self.canvas.coords(self.item, x, y)
            self.shown = (x, y, width, height)
            if dims_changed:
                self.root.after_idle(self._fit_window_to_video)
    
        # ----------------------------------------------------------------- input
        def _point(self, x: int, y: int):
            sx, sy, sw, sh = self.shown
            if not sw or not sh:
                return None
            nx = (x - sx) / sw
            ny = (y - sy) / sh
            if nx < 0 or nx > 1 or ny < 0 or ny > 1:
                return None
            return nx, ny
    
        def _mouse(self, kind: str, point, btn: int | None = None, dy: int | None = None) -> None:
            params = {"kind": kind}
            if point is not None:
                params["nx"], params["ny"] = point
            if btn is not None:
                params["btn"] = btn
            if dy is not None:
                params["dy"] = dy
            if self.target:
                params["win"] = self.target["win"]
            self.link.send("input.mouse", **params)
    
        def _on_motion(self, event) -> None:
            if self._locked:
                return  # режим блокировки: движение мыши не отправляем
            point = self._point(event.x, event.y)
            if point:
                self._mouse("move", point)

        def _on_button(self, event, down: bool, btn: int) -> None:
            if self._locked:
                return  # режим блокировки: клики не отправляем
            point = self._point(event.x, event.y)
            if not point:
                return
            self.canvas.focus_set()
            self._mouse("down" if down else "up", point, btn=btn)

        def _on_wheel(self, event, dy: int) -> None:
            if self._locked:
                return  # режим блокировки: колесо не отправляем
            point = self._point(event.x, event.y)
            self._mouse("wheel", point, dy=dy)

        def _combo(self, event):
            key = event.keysym
            if key in SHIFTED:
                return SHIFTED[key]
            if key not in NAMED and len(key) != 1:
                return None
            mods = []
            state = event.state
            if state & 0x4:
                mods.append("ctrl")
            if state & 0x8:
                mods.append("alt")
            if state & 0x40:
                mods.append("super")
            if state & 0x1 and len(key) == 1:
                mods.append("shift")
            if len(key) == 1:
                key = key.lower()
            return "+".join(mods + [key])
    
        def _on_keydown(self, event):
            # Ctrl+Alt+I — показать/скрыть панель
            combo = self._combo(event)
            if combo == "ctrl+alt+i":
                self._toggle_toolbar()
                return
            # Ctrl+Alt+L — включить/выключить блокировку мыши
            if combo == "ctrl+alt+l":
                self._toggle_lock()
                return
            if event.keysym == "F11":
                self._toggle_fullscreen()
                return
            if event.keysym == "Escape":
                fullscreen = bool(self.root.attributes("-fullscreen"))
                if fullscreen:
                    self._toggle_fullscreen()
                    return
                # В обычном режиме Esc — показать/скрыть панель
                self._toggle_toolbar()
                return
            key = event.keysym.lower()
            if combo:
                self._key(combo, True)
    
        def _on_keyup(self, event):
            combo = self._combo(event)
            if combo:
                self._key(combo, False)
    
        def _key(self, combo: str, down: bool) -> None:
            params = {"combo": combo, "down": down}
            if self.target:
                params["win"] = self.target["win"]
            self.link.send("input.key", **params)
    
        # ------------------------------------------------------------------ run
        def run(self) -> None:
            self.root.after(10, self._tick)
            self.root.mainloop()
    
        def close(self) -> None:
            self._closing = True
            if self._live is not None:
                self._live.clear()
            sock = self._vsock
            if sock is not None:
                try:
                    sock.shutdown(0)
                except OSError:
                    pass
            self.link.close()
            self.root.destroy()
else:
    Viewer = None  # noqa: F811
