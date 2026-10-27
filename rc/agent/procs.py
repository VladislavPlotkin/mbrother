"""Process listing for the "task manager" panel.

Only the processes of the logged in user are shown - the same idea as the
"Applications" tab of the Windows task manager, plus a full list of the
user's processes.
"""
from __future__ import annotations

import os
import pwd
import time

import psutil


class Procs:
    def __init__(self, uid: int | None = None):
        self.uid = os.getuid() if uid is None else int(uid)
        try:
            self.username = pwd.getpwuid(self.uid).pw_name
        except KeyError:  # pragma: no cover
            self.username = str(self.uid)
        self._cpu: dict[int, tuple[float, float]] = {}

    def list(self, only_user: bool = True, app_pids: set[int] | None = None) -> list[dict]:
        app_pids = app_pids or set()
        now = time.monotonic()
        rows = []
        for proc in psutil.process_iter(["pid", "name", "username", "memory_info", "create_time"]):
            try:
                info = proc.info
                username = info.get("username") or ""
                if only_user and username != self.username:
                    continue
                pid = info["pid"]
                cpu = self._cpu_percent(proc, pid, now)
                mem = info.get("memory_info")
                rss = int(mem.rss) if mem is not None else 0
                try:
                    cmdline = " ".join(proc.cmdline())
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmdline = ""
                created = info.get("create_time") or 0
                rows.append(
                    {
                        "pid": pid,
                        "name": info.get("name") or "",
                        "user": username,
                        "cpu": round(cpu, 1),
                        "mem": rss,
                        "cmdline": cmdline[:400],
                        "uptime": max(0, int(now - created)) if created else 0,
                        "is_app": pid in app_pids,
                        "threads": proc.num_threads(),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        rows.sort(key=lambda r: (not r["is_app"], -r["cpu"], r["name"].lower()))
        return rows

    def _cpu_percent(self, proc, pid: int, now: float) -> float:
        try:
            times = proc.cpu_times()
            used = times.user + times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0
        previous = self._cpu.get(pid)
        self._cpu[pid] = (now, used)
        if not previous:
            return 0.0
        prev_time, prev_used = previous
        delta = now - prev_time
        if delta <= 0:
            return 0.0
        return max(0.0, (used - prev_used) / delta * 100.0)

    def kill(self, pid: int, force: bool = False) -> bool:
        """Terminate a process (SIGTERM first, SIGKILL when forced)."""
        try:
            proc = psutil.Process(int(pid))
        except psutil.NoSuchProcess:
            return False
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    proc.kill()
            return True
        except psutil.AccessDenied:
            return False
