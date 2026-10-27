"""Run the control panel: ``python3 -m rc.client``."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app import serve
from .wake import load_settings

DEFAULT_SETTINGS = Path.home() / ".config" / "rc" / "client.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="rc control panel (runs on the controlling PC)")
    parser.add_argument("--agent-host", default=None, help="address of the controlled PC")
    parser.add_argument("--agent-port", type=int, default=None)
    parser.add_argument("--bind", default="127.0.0.1", help="address the panel listens on")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
    parser.add_argument("--print-config", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings(args.settings)
    agent_host = args.agent_host or settings.get("agent_host")
    if not agent_host:
        parser.error("no agent host configured (use --agent-host or %s)" % args.settings)
    agent_port = args.agent_port or int(settings.get("agent_port", 7777))
    settings["agent_host"] = agent_host
    settings["agent_port"] = agent_port

    if args.print_config:
        print(json.dumps(settings, indent=2, ensure_ascii=False))
        return 0

    serve(
        agent_host=agent_host,
        agent_port=agent_port,
        bind=args.bind,
        port=args.port,
        settings=settings,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
