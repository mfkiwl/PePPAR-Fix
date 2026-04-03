"""Helpers for exclusive access to single-user device streams.

For tty devices (serial ports), use TIOCEXCL ioctl — kernel-enforced,
auto-released on fd close (even on crash/SIGKILL).

For non-tty devices (kernel GNSS /dev/gnss*), TIOCEXCL is not available.
Fall back to a PID-validated lock file: check if the PID in the lock
is still alive before declaring a conflict.  Stale locks from crashed
processes are automatically reclaimed.
"""

from __future__ import annotations

import fcntl
import os
import re
import signal

_LOCK_DIR = "/tmp/peppar-fix-locks"


def acquire_device_lock(device_path: str) -> tuple[int, str]:
    """Acquire an advisory nonblocking lock for a device path.

    Uses flock() with PID validation — if the lock is held but the
    owning PID is dead, the lock is reclaimed automatically.
    """
    os.makedirs(_LOCK_DIR, exist_ok=True)
    real = os.path.realpath(device_path)
    lock_name = re.sub(r"[^A-Za-z0-9._-]+", "_", real.lstrip("/"))
    lock_path = os.path.join(_LOCK_DIR, f"{lock_name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Lock is held — check if the owner is still alive
        try:
            content = os.pread(fd, 256, 0).decode(errors='replace')
            m = re.search(r'pid=(\d+)', content)
            if m:
                owner_pid = int(m.group(1))
                try:
                    os.kill(owner_pid, 0)  # check if alive
                except ProcessLookupError:
                    # Owner is dead — reclaim the stale lock
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except PermissionError:
                    pass  # process exists but owned by another user
                else:
                    os.close(fd)
                    raise RuntimeError(
                        f"{device_path} is already in use by pid {owner_pid} "
                        f"(lock: {lock_path})"
                    ) from None
            else:
                os.close(fd)
                raise RuntimeError(
                    f"{device_path} is already in use by another process "
                    f"(lock: {lock_path})"
                ) from None
        except RuntimeError:
            raise
        except Exception:
            os.close(fd)
            raise RuntimeError(
                f"{device_path} is already in use "
                f"(lock: {lock_path})"
            ) from None

    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()} device={real}\n".encode())
    return fd, lock_path


def release_device_lock(lock_fd: int | None) -> None:
    """Release a lock acquired by acquire_device_lock()."""
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
