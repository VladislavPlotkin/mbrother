"""Command line helper.

    rc panel                 start the control panel (default)
    rc ctl <command> [...]   run a single command against the agent
    rc wake                  wake the controlled machine
    rc video                 open the video client in a browser window
    rc viewer                open the native floating viewer window
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from .client.agent_link import AgentLink
from .client.wake import load_settings, wake as do_wake

DEFAULT_SETTINGS = "~/.config/rc/client.json"


def _settings(path: str) -> dict:
    return load_settings(path)


def cmd_panel(args, settings: dict) -> int:
    from .client.app import serve

    agent_host = args.agent_host or settings.get("agent_host")
    if not agent_host:
        print("no agent host configured", file=sys.stderr)
        return 2
    serve(
        agent_host=agent_host,
        agent_port=args.agent_port or int(settings.get("agent_port", 7777)),
        bind=args.bind,
        port=args.port,
        settings=settings,
        settings_path=args.settings,
    )
    return 0


def cmd_ctl(args, settings: dict) -> int:
    agent_host = args.agent_host or settings.get("agent_host")
    if not agent_host:
        print("no agent host configured", file=sys.stderr)
        return 2
    link = AgentLink(agent_host, args.agent_port or int(settings.get("agent_port", 7777)))
    if not link.connect():
        print("cannot reach the agent at %s" % agent_host, file=sys.stderr)
        return 1
    params = {}
    for item in args.params:
        if "=" in item:
            key, value = item.split("=", 1)
            try:
                params[key] = json.loads(value)
            except ValueError:
                params[key] = value
        else:
            params["value"] = item
    try:
        if args.command == "input.key" and "down" not in params:
            # Without an explicit "down" the key is tapped: pressed and
            # released again, so no modifier stays stuck.
            link.call("input.key", down=True, **params)
            result = link.call("input.key", down=False, **params)
        else:
            result = link.call(args.command, **params)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        link.close()
    return 0


def cmd_wake(args, settings: dict) -> int:
    try:
        print(do_wake(settings))
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


def cmd_video(args, settings: dict) -> int:
    host = args.bind or settings.get("bind", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    url = "http://%s:%d/video" % (host, args.port)
    print(url)
    webbrowser.open(url)
    return 0


def cmd_viewer(args, settings: dict) -> int:
    from .client.viewer import Viewer

    agent_host = args.agent_host or settings.get("agent_host")
    if not agent_host:
        print("no agent host configured", file=sys.stderr)
        return 2
    viewer = Viewer(
        agent_host,
        args.agent_port or int(settings.get("agent_port", 7777)),
        source=args.source,
        fullscreen=args.fullscreen,
    )
    viewer.run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rc", description=__doc__)
    parser.add_argument("--agent-host", default=None)
    parser.add_argument("--agent-port", type=int, default=None)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--settings", default=DEFAULT_SETTINGS)
    sub = parser.add_subparsers(dest="subcommand")

    sub.add_parser("panel", help="start the control panel")
    ctl = sub.add_parser("ctl", help="run a single command against the agent")
    ctl.add_argument("command")
    ctl.add_argument("params", nargs="*")
    sub.add_parser("wake", help="wake the controlled machine")
    sub.add_parser("video", help="open the video client")
    viewer = sub.add_parser("viewer", help="open the native floating viewer window")
    viewer.add_argument("--source", default="screen",
                        help="screen or win:<id> (default: screen)")
    viewer.add_argument("--fullscreen", action="store_true")

    args = parser.parse_args(argv)
    settings = _settings(args.settings)
    handlers = {
        "panel": cmd_panel,
        "ctl": cmd_ctl,
        "wake": cmd_wake,
        "video": cmd_video,
        "viewer": cmd_viewer,
    }
    handler = handlers.get(args.subcommand or "panel")
    return handler(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
