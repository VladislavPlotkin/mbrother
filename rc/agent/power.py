"""Power control of the target machine.

Only the *off* and *reboot* directions live here: they are executed by the
agent, which runs on the machine being controlled.

The "power on" direction cannot live on the agent (the agent is dead when the
machine is off), so it is implemented in the client - see
``rc/client/wake.py``.
"""
from __future__ import annotations

import subprocess
import threading
import time


class Power:
    def __init__(self, sudo: bool = True):
        self.sudo = sudo
        self._lock = threading.Lock()

    def _run(self, args: list[str]) -> str:
        cmd = (["sudo", "-n"] if self.sudo else []) + args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
        return (proc.stdout or "").strip()

    def off(self, delay: float = 0.6) -> str:
        """Power off the machine.  The reply is sent before the machine dies."""
        with self._lock:
            threading.Timer(delay, self._shutdown, args=("poweroff",)).start()
        return "poweroff scheduled"

    def reboot(self, delay: float = 0.6) -> str:
        with self._lock:
            threading.Timer(delay, self._shutdown, args=("reboot",)).start()
        return "reboot scheduled"

    def _shutdown(self, action: str) -> None:
        try:
            self._run(["systemctl", action])
        except Exception:
            pass
