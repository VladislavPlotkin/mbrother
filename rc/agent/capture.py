"""Screen capture for the agent.

A single ``ffmpeg`` process grabs the X11 screen and writes an MJPEG stream
to stdout.  The stream is split into individual JPEG frames and fanned out to
subscribers - one subscriber is one connected video client.

Everything is software encoded (the target machine has no NVENC), which is
cheap enough at 1280x720: roughly 40-60 kB per frame.
"""
from __future__ import annotations

import io
import os
import queue
import select
import subprocess
import threading
import time

from .x11 import X11

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

try:  # optional - only needed for the "window only" crop mode
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


class JpegSplitter:
    """Split a concatenated MJPEG byte stream into individual JPEG frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames = []
        while True:
            frame = self._next_frame()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _next_frame(self) -> bytes | None:
        buf = self._buf
        start = buf.find(SOI)
        if start < 0:
            # Keep the tail: the SOI marker may be split across two reads.
            if len(buf) > 4096:
                del buf[:-2]
            return None
        if start > 0:
            del buf[:start]
        end = buf.find(EOI, 2)
        while end >= 0:
            following = bytes(buf[end + 2:end + 4])
            if following in (b"", SOI):
                frame = bytes(buf[:end + 2])
                del buf[:end + 2]
                return frame
            # False positive: an EOI marker inside the entropy coded data.
            end = buf.find(EOI, end + 2)
        return None


class Subscriber:
    """One video consumer.  ``target`` is None for the full screen."""

    _next_id = 1
    _id_lock = threading.Lock()

    def __init__(self, target=None):
        with Subscriber._id_lock:
            self.id = Subscriber._next_id
            Subscriber._next_id += 1
        self.target = target
        self.queue: queue.Queue = queue.Queue(maxsize=3)
        self.closed = False
        self.capturer: "WindowCapturer | None" = None

    def put(self, item) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            try:
                self.queue.get_nowait()  # drop the oldest frame
                self.queue.put_nowait(item)
            except queue.Empty:  # pragma: no cover
                pass

    def get(self, timeout: float = 1.0):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None


def pil_quality(qscale: int) -> int:
    """Translate an ffmpeg qscale (1..31, lower is better) to a PIL quality.

    PIL expects 1..95 (higher is better), so passing the raw qscale produces
    a heavily smeared picture.
    """
    return max(35, min(95, 100 - int(qscale) * 4))


def terminate(proc) -> None:
    """Stop an ffmpeg process and reap it (no zombies left behind)."""
    if proc is None:
        return
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # pragma: no cover
            pass
    try:
        proc.terminate()
    except Exception:  # pragma: no cover
        pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:  # pragma: no cover
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # pragma: no cover
        pass


class WindowCapturer:
    """A dedicated ffmpeg process that grabs one window directly.

    Cropping a window out of the compressed full screen frame means the
    picture is JPEG encoded twice and then scaled up by the viewer - the
    result looks blurry.  Grabbing the window region from X11 directly keeps
    the native resolution and encodes it only once.
    """

    def __init__(self, capture: "Capture", subscriber: Subscriber, win_id: int):
        self.capture = capture
        self.subscriber = subscriber
        self.win_id = win_id
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="capture-win-%d" % win_id, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_geo = None
        proc = None
        splitter = JpegSplitter()
        try:
            while not self._stop.is_set() and not self.subscriber.closed:
                geo = self.capture.x11.geometry(self.win_id, max_age=0.5)
                if not geo:
                    time.sleep(0.5)
                    continue
                x, y, w, h = geo
                if w <= 0 or h <= 0:
                    time.sleep(0.5)
                    continue
                if geo != last_geo:
                    last_geo = geo
                    proc = self._spawn(proc, x, y, w, h)
                    if proc is None:
                        time.sleep(1.0)
                        continue
                    splitter = JpegSplitter()
                chunk = self._read(proc)
                if chunk is None:
                    break
                if not chunk:
                    time.sleep(0.05)
                    continue
                for frame in splitter.feed(chunk):
                    self.capture.publish(frame, self.subscriber)
        except Exception:
            pass
        finally:
            self._kill(proc)

    @staticmethod
    def _read(proc: subprocess.Popen) -> bytes | None:
        """Read whatever ffmpeg produced, without blocking forever."""
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        except (OSError, ValueError):
            return None
        if not ready:
            return b""
        try:
            return os.read(proc.stdout.fileno(), 65536)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _kill(proc) -> None:
        terminate(proc)

    def _spawn(self, old, x: int, y: int, w: int, h: int):
        terminate(old)
        display = self.capture.display_name
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "x11grab",
            "-draw_mouse", "1",
            "-framerate", str(self.capture.fps),
            "-video_size", "%dx%d" % (w, h),
            "-i", "%s+%d,%d" % (display, x, y),
            "-q:v", str(self.capture.quality),
            "-f", "mjpeg",
            "pipe:1",
        ]
        try:
            return subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            return None


class Capture:
    """Owns the ffmpeg process and the subscriber list."""

    def __init__(
        self,
        x11: X11,
        fps: int = 30,
        quality: int = 3,
        display_name: str | None = None,
        codec: str = "mjpeg",
        h264_qp: int = 23,
    ):
        self.x11 = x11
        self.fps = fps
        self.quality = quality
        self.display_name = display_name or os.environ.get("DISPLAY", ":0.0")
        self.codec = codec if codec in ("mjpeg", "h264", "h265") else "mjpeg"
        self.h264_qp = max(16, min(40, int(h264_qp)))
        self.gop = self.fps * 2  # ключевая картинка раз в 2 секунды
        self._lock = threading.Lock()
        self._subs: dict[int, Subscriber] = {}
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self.seq = 0
        self.latest_frame: tuple[int, bytes, float] | None = None
        self.error: str | None = None
        self._size = (0, 0)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc:
            terminate(proc)
        with self._lock:
            subs = list(self._subs.values())
        for sub in subs:
            if sub.capturer is not None:
                sub.capturer.stop()

    def set_quality(self, quality: int) -> None:
        if self.codec in ("h264", "h265"):
            return  # качество H.264/H.265 задаётся qp из конфига
        quality = max(1, min(15, int(quality)))
        if quality != self.quality:
            self.quality = quality
            self._restart()

    # ----------------------------------------------------------- subscribers
    def subscribe(self, target=None) -> Subscriber:
        sub = Subscriber(target)
        with self._lock:
            self._subs[sub.id] = sub
        win_id = self._window_id(target)
        if win_id:
            sub.capturer = WindowCapturer(self, sub, win_id)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        sub.closed = True
        with self._lock:
            self._subs.pop(sub.id, None)
        if sub.capturer is not None:
            sub.capturer.stop()
            sub.capturer = None

    @staticmethod
    def _window_id(target) -> int | None:
        if not target:
            return None
        if isinstance(target, dict):
            win_id = target.get("win")
        else:
            win_id = target
        try:
            win_id = int(win_id)
        except (TypeError, ValueError):
            return None
        return win_id or None

    # -------------------------------------------------------------- internals
    def _needs_screen(self) -> bool:
        """True while at least one subscriber needs the full-screen capture."""
        with self._lock:
            return any(sub.capturer is None for sub in self._subs.values())

    def _command(self, size: tuple[int, int]) -> list[str]:
        width, height = size
        common = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "x11grab",
            "-draw_mouse", "1",
            "-framerate", str(self.fps),
            "-video_size", "%dx%d" % (width, height),
            "-i", "%s+0,0" % self.display_name,
        ]
        if self.codec in ("h264", "h265"):
            # Аппаратное кодирование через VAAPI; сырой Annex-B поток
            # без контейнера (муксер добавлял бы ~25 мс задержки).
            is_h265 = self.codec == "h265"
            return common + [
                "-vaapi_device", "/dev/dri/renderD128",
                "-vf", "format=nv12,hwupload",
                "-c:v", "hevc_vaapi" if is_h265 else "h264_vaapi",
                "-qp", str(self.h264_qp),
                "-bf", "0",
                "-g", str(self.gop),
                "-async_depth", "1",
                "-bsf:v", "hevc_mp4toannexb" if is_h265 else "h264_mp4toannexb",
                "-f", "hevc" if is_h265 else "h264",
                "pipe:1",
            ]
        return common + [
            "-q:v", str(self.quality),
            "-f", "mjpeg",
            "pipe:1",
        ]

    def _spawn(self, size: tuple[int, int]) -> subprocess.Popen:
        self._proc = subprocess.Popen(
            self._command(size),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._size = size
        return self._proc

    def _restart(self) -> None:
        proc = self._proc
        if proc:
            terminate(proc)
        self._proc = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._needs_screen():
                time.sleep(0.25)  # никто не смотрит — ffmpeg не держим
                continue
            try:
                size = self.x11.screen_size()
            except Exception:
                size = (1280, 720)
            try:
                proc = self._spawn(size)
            except OSError as exc:
                self.error = "cannot start ffmpeg: %s" % exc
                time.sleep(2.0)
                continue
            splitter = JpegSplitter() if self.codec == "mjpeg" else None
            last_size_check = time.monotonic()
            idle_since = None
            try:
                while not self._stop.is_set():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    if splitter is None:
                        self._publish(chunk)
                    else:
                        for frame in splitter.feed(chunk):
                            self._publish(frame)
                    now = time.monotonic()
                    if self._needs_screen():
                        idle_since = None
                    elif idle_since is None:
                        idle_since = now
                    elif now - idle_since > 2.0:
                        break  # подписчиков нет — гасим ffmpeg до следующего

                    if now - last_size_check > 5.0:
                        last_size_check = now
                        try:
                            if self.x11.screen_size() != size:
                                self._restart()
                                break
                        except Exception:
                            pass
            finally:
                stderr = b""
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
                try:
                    if proc.stderr is not None:
                        stderr = proc.stderr.read() or b""
                except Exception:
                    pass
                terminate(proc)
            if not self._stop.is_set():
                self.error = stderr.decode("utf-8", "replace").strip()[:400]
                time.sleep(0.5)

    def _publish(self, jpeg: bytes) -> None:
        self.seq += 1
        stamp = time.monotonic()
        self.latest_frame = (self.seq, jpeg, stamp)
        with self._lock:
            subs = list(self._subs.values())
        for sub in subs:
            if sub.capturer is not None:
                continue  # the window has its own capture process
            sub.put((self.seq, jpeg, stamp))

    def publish(self, jpeg: bytes, subscriber: Subscriber) -> None:
        """Publish a frame to a single subscriber (window capture)."""
        self.seq += 1
        stamp = time.monotonic()
        self.latest_frame = (self.seq, jpeg, stamp)
        subscriber.put((self.seq, jpeg, stamp))

    # ------------------------------------------------------------------ crop
    def crop_for(self, sub: Subscriber, jpeg: bytes) -> bytes:
        """Crop a frame to the requested window (falls back to the full frame)."""
        if sub.capturer is not None:
            return jpeg  # the frame is already the window
        if not sub.target or Image is None:
            return jpeg
        win_id = sub.target.get("win") if isinstance(sub.target, dict) else sub.target
        if not win_id:
            return jpeg
        geo = self.x11.geometry(int(win_id), max_age=0.3)
        if not geo:
            return jpeg
        x, y, w, h = geo
        if w <= 0 or h <= 0:
            return jpeg
        try:
            image = Image.open(io.BytesIO(jpeg)).convert("RGB")
            full_w, full_h = image.size
            left = max(0, min(x, full_w))
            top = max(0, min(y, full_h))
            right = max(left + 1, min(x + w, full_w))
            bottom = max(top + 1, min(y + h, full_h))
            cropped = image.crop((left, top, right, bottom))
            out = io.BytesIO()
            cropped.save(out, "JPEG", quality=pil_quality(self.quality))
            return out.getvalue()
        except Exception:
            return jpeg
