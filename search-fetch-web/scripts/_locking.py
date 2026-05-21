"""Cross-platform advisory file locking for search-fetch scripts.

Uses fcntl on Unix and msvcrt on Windows.
"""

import os
import sys

if sys.platform == "win32":
    import msvcrt
    import time as _time

    def flock_ex(fd: int) -> None:
        """Acquire exclusive advisory lock (blocking)."""
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
        for _ in range(3600):
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                _time.sleep(0.05)
        raise OSError("file lock acquisition timed out")

    def flock_un(fd: int) -> None:
        """Release exclusive advisory lock."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def flock_ex(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def flock_un(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
