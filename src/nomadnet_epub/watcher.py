from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)


def _is_epub_path(path: str) -> bool:
    return str(path).lower().endswith(".epub")


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[], None], debounce: float):
        super().__init__()
        self._callback = callback
        self._debounce = debounce
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        log.info("EPUB change detected; running update")
        try:
            self._callback()
        except Exception:
            log.exception("Update after EPUB change failed")

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed"):
            return
        src = str(getattr(event, "src_path", "") or "")
        dest = str(getattr(event, "dest_path", "") or "")
        if _is_epub_path(src) or _is_epub_path(dest):
            self._schedule()


class EpubWatcher:
    def __init__(
        self,
        epubs_dir: Path,
        on_change: Callable[[], None],
        *,
        debounce: float = 2.0,
    ):
        self.epubs_dir = epubs_dir
        self.on_change = on_change
        self.debounce = debounce
        self._observer: Observer | None = None

    def start(self) -> None:
        self.epubs_dir.mkdir(parents=True, exist_ok=True)
        handler = _DebouncedHandler(self.on_change, self.debounce)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.epubs_dir), recursive=True)
        self._observer.start()
        log.info("Watching %s", self.epubs_dir)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def run_forever(self) -> None:
        assert self._observer is not None
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
