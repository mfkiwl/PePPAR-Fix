"""Helpers for exclusive access to single-user device streams."""

from __future__ import annotations

import fcntl
import os
import re

_LOCK_DIR = "/tmp/peppar-fix-locks"


def acquire_device_lock(device_path: str) -> tuple[int, str]:
    """Acquire an advisory nonblocking lock for a device path."""
    os.makedirs(_LOCK_DIR, exist_ok=True)
    real = os.path.realpath(device_path)
    lock_name = re.sub(r"[^A-Za-z0-9._-]+", "_", real.lstrip("/"))
    lock_path = os.path.join(_LOCK_DIR, f"{lock_name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError(
            f"{device_path} is already in use by another peppar-fix process "
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
