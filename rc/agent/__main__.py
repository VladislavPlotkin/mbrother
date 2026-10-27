"""Entry point of the agent: ``python3 -m rc.agent``."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

import logging

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

from .. import __version__
from .api import Api
from .capture import Capture
from .clip import Clip
from .files import FileStore
from .power import Power
from .procs import Procs
from .server import AgentServer
from .x11 import X11

DEFAULT_CONFIG = Path.home() / ".config" / "rc" / "agent.json"


def load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="rc agent (runs on the controlled PC)")
    parser.add_argument("--host", default=None, help="address to listen on")
    parser.add_argument("--port", type=int, default=None, help="control port")
    parser.add_argument("--display", default=None, help="X display to control (e.g. :0.0)")
    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument("--quality", type=int, default=None, help="JPEG quality 1..15")
    parser.add_argument(
        "--codec", default=None, choices=["mjpeg", "h264", "h265"],
        help="video codec: mjpeg (default, lowest latency), h264 or h265 (VAAPI GPU)")
    parser.add_argument("--h264-qp", type=int, default=None, help="H.264/H.265 quantizer 16..40")
    parser.add_argument("--share", default=None, help="file exchange directory")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--no-capture", action="store_true", help="do not grab the screen")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    host = args.host or config.get("host", "0.0.0.0")
    port = args.port or int(config.get("port", 7777))
    display_name = args.display or config.get("display") or os.environ.get("DISPLAY", ":0.0")
    fps = args.fps or int(config.get("fps", 30))
    quality = args.quality or int(config.get("quality", 3))
    codec = args.codec or config.get("codec", "mjpeg")
    h264_qp = args.h264_qp or int(config.get("h264_qp", 23))
    share = args.share or config.get("share")

    os.environ["DISPLAY"] = display_name

    x11 = X11(display_name)
    capture = Capture(
        x11,
        fps=fps,
        quality=quality,
        display_name=display_name,
        codec=codec,
        h264_qp=h264_qp,
    )
    api = Api(
        x11=x11,
        capture=capture,
        procs=Procs(),
        power=Power(sudo=True),
        clip=Clip(),
        files=FileStore(share),
        version=__version__,
    )
    if not args.no_capture:
        capture.start()

    server = AgentServer(api, host=host, port=port)
    print(
        "rc agent %s listening on %s:%d (display %s, %dx%d)"
        % (
            __version__,
            host,
            port,
            display_name,
            *x11.screen_size(),
        ),
        flush=True,
    )

    def _shutdown(signum, frame):  # pragma: no cover
        capture.stop()
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        capture.stop()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
