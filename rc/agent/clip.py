"""Text clipboard bridge (X11 CLIPBOARD selection via xclip)."""
from __future__ import annotations

import subprocess


class Clip:
    def __init__(self, selection: str = "clipboard"):
        self.selection = selection
        self._owner = None

    def _tool(self) -> str | None:
        for tool in ("xclip", "xsel"):
            try:
                subprocess.run(
                    [tool, "-version"], capture_output=True, timeout=5, check=False
                )
                return tool
            except FileNotFoundError:
                continue
        return None

    def get(self) -> str:
        tool = self._tool()
        if tool is None:
            raise RuntimeError("neither xclip nor xsel is installed")
        if tool == "xclip":
            cmd = ["xclip", "-selection", self.selection, "-o"]
        else:
            cmd = ["xsel", "--clipboard", "--output"]
        proc = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace")

    def _stop_owner(self) -> None:
        owner, self._owner = self._owner, None
        if owner is None:
            return
        try:
            owner.kill()
            owner.wait(timeout=2)
        except Exception:
            pass

    def set(self, text: str) -> bool:
        """Put *text* into the clipboard.

        The tool that owns the selection has to stay alive, so it is started
        as a background process and kept as ``self._owner``.  Killing it when
        the next value arrives hands the selection over to the new owner.
        """
        tool = self._tool()
        if tool is None:
            raise RuntimeError("neither xclip nor xsel is installed")
        if tool == "xclip":
            cmd = ["xclip", "-selection", self.selection, "-i"]
        else:
            cmd = ["xsel", "--clipboard", "--input"]
        self._stop_owner()
        try:
            owner = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            assert owner.stdin is not None
            owner.stdin.write(text.encode("utf-8"))
            owner.stdin.flush()
            owner.stdin.close()
        except OSError:
            owner.kill()
            return False
        self._owner = owner
        return True
