from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


class InstanceLock:
    """One live caller process per state directory; workers use database job leases."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> InstanceLock:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError("Another call-secretary instance owns the call channel") from None
        self.fd = fd
        return self

    def __exit__(self, *args: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
