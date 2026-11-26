import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class MT5WorkerLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def acquire(self, stop_event: threading.Event) -> Iterator[bool]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()

            acquired = False
            while not stop_event.is_set():
                handle.seek(0)
                try:
                    self._lock(handle)
                except (BlockingIOError, PermissionError):
                    stop_event.wait(0.5)
                else:
                    acquired = True
                    break

            try:
                yield acquired
            finally:
                if acquired:
                    handle.seek(0)
                    self._unlock(handle)
        finally:
            handle.close()

    @staticmethod
    def _lock(handle) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
