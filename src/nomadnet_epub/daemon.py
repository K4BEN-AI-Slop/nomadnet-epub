from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)


class NomadDaemon:
    """Own a `nomadnet --daemon` child process and restart it on demand."""

    def __init__(self, config_dir: Path, *, console: bool = True):
        self.config_dir = config_dir
        self.console = console
        self._proc: subprocess.Popen | None = None

    @property
    def pid(self) -> int | None:
        if self._proc and self._proc.poll() is None:
            return self._proc.pid
        return None

    def _nomadnet_bin(self) -> str:
        path = shutil.which("nomadnet")
        if not path:
            raise FileNotFoundError(
                "nomadnet not found on PATH. Install with: pip install nomadnet"
            )
        return path

    def start(self) -> None:
        if self.pid is not None:
            return
        cmd = [
            self._nomadnet_bin(),
            "--daemon",
            "--config",
            str(self.config_dir),
        ]
        if self.console:
            cmd.append("--console")
        log.info("Starting NomadNet: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd)
        time.sleep(0.5)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"NomadNet exited immediately with code {self._proc.returncode}"
            )
        log.info("NomadNet running (pid %s)", self._proc.pid)

    def stop(self, timeout: float = 15.0) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        log.info("Stopping NomadNet (pid %s)", proc.pid)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("NomadNet did not exit; killing")
            proc.kill()
            proc.wait(timeout=5)

    def restart(self) -> None:
        self.stop()
        self.start()
