"""TelegramDaemon — lifecycle manager for the standalone telegram_capture.py.

Kept as a separate subprocess (not a Flet-loop thread) so a crash can't take the
UI down, reusing the proven capture script verbatim. The app just starts/stops
it and surfaces status. See telegram_capture.py for the long-poll capture itself.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


class TelegramDaemon:
    """Lifecycle manager for the standalone telegram_capture.py daemon.

    Kept as a *separate subprocess* (not a thread in the Flet event loop) on
    purpose: a crash can't take down the UI, and we reuse the proven capture
    script verbatim. The app just starts/stops it and surfaces status — same
    "Workbench is a hub, not an editor" framing as the rest of the app. Capture
    runs only while this process keeps the daemon alive (stopped on app exit);
    an always-on systemd service can layer on later without conflicting.
    """

    SCRIPT = Path(__file__).resolve().parent / "telegram_capture.py"
    LOG_PATH = Path.home() / ".workbench" / "telegram.log"

    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self._logf = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> Optional[str]:
        """Spawn the daemon under the app's own interpreter (so it shares the
        venv that already has httpx). Returns an error string, or None on success
        / already-running."""
        if self.is_running():
            return None
        try:
            self.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._logf = open(self.LOG_PATH, "a", encoding="utf-8")
            self.proc = subprocess.Popen(
                [sys.executable, str(self.SCRIPT)],
                start_new_session=True,
                stdout=self._logf, stderr=self._logf,
                env={**os.environ, "WORKBENCH_TELEGRAM_VERBOSE": "1"},
            )
            return None
        except Exception as ex:
            return str(ex)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None
        if self._logf:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None

    def tail_log(self, n: int = 10) -> str:
        try:
            lines = self.LOG_PATH.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[-n:])
        except Exception:
            return ""
