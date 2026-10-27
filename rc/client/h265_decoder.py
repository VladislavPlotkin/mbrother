"""H.265/HEVC stream decoder for client — no Tkinter dependency.

Decodes a raw Annex-B H.265 byte stream into PIL Image frames using PyAV.
Used by the video loop in viewer.py when the agent sends H.265 instead of
JPEG.  The decoder is created lazily (per subscription) so a plain MJPEG
viewer never pays the PyAV import cost.
"""
from __future__ import annotations


class H265StreamDecoder:
    """Decodes raw Annex-B H.265 byte stream into PIL images (via PyAV)."""

    def __init__(self) -> None:
        import av
        ctx = av.CodecContext.create("hevc", "r")
        ctx.thread_count = 1  # avoid frame-thread buffering delay
        ctx.options = {"flags": "low_delay"}
        self._ctx = ctx
        self._last_image: object = None
        self._last_frame: object = None

    def feed(self, data: bytes) -> object:
        """Feed Annex-B data, return the latest decoded PIL Image or None."""
        try:
            packets = list(self._ctx.parse(data))
        except ValueError:
            return self._last_image  # garbage before SPS/IDR — wait for key frame

        for packet in packets:
            try:
                for frame in self._ctx.decode(packet):
                    self._last_frame = frame
            except ValueError:
                continue

        frame = self._last_frame
        if frame is None:
            return self._last_image

        try:
            self._last_image = frame.to_image()
            return self._last_image
        except Exception:
            return self._last_image
