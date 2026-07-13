"""In-process mock implementation of the :mod:`file_p2p` extension.

It has the same public request methods as the native extension.  A read queues
bytes from a normal file, and ``drain_read`` makes the queued bytes visible in
simulated device memory addressed by the request's ``addr`` value.
"""

import errno
import os
from typing import Dict, List, Optional, Tuple

_next_fd = 10_000
_open_fds = set()
_pending: Dict[int, List[Tuple[int, bytes]]] = {}
_buffers: Dict[int, bytes] = {}


def _error_number(exc: OSError) -> int:
    return -(exc.errno or errno.EIO)


def _valid_fd(dev_fd: int) -> bool:
    return isinstance(dev_fd, int) and dev_fd in _open_fds


def _queue_read(dev_fd: int, file_name: str, bdev_offset: int, addr: int, size: int) -> int:
    if not _valid_fd(dev_fd):
        return -errno.EBADF
    if not isinstance(file_name, str) or not isinstance(bdev_offset, int) or not isinstance(addr, int) or not isinstance(size, int):
        return -errno.EINVAL
    if bdev_offset < 0 or addr < 0 or size < 0:
        return -errno.EINVAL

    try:
        with open(file_name, "rb") as source:
            source.seek(0, os.SEEK_END)
            if bdev_offset + size > source.tell():
                return -errno.ERANGE
            source.seek(bdev_offset)
            payload = source.read(size)
    except OSError as exc:
        return _error_number(exc)

    _pending[dev_fd].append((addr, payload))
    return 0


def new_p2p_fd() -> int:
    """Create a mock P2P handle and return it."""
    global _next_fd
    dev_fd = _next_fd
    _next_fd += 1
    _open_fds.add(dev_fd)
    _pending[dev_fd] = []
    return dev_fd


def close_p2p_fd(dev_fd: int) -> None:
    """Close a mock handle and discard queued, undrained operations."""
    _open_fds.discard(dev_fd)
    _pending.pop(dev_fd, None)


def read_file(dev_fd: int, file_name: str, bdev_name: str, bdev_offset: int,
              addr: int, size: int, devid: int, vfid: int) -> int:
    """Queue one ordinary-file read using the native extension's signature.

    ``bdev_name``, ``devid``, and ``vfid`` are accepted for API compatibility;
    the mock uses ``file_name`` and ``bdev_offset`` as its data source.
    """
    del bdev_name, devid, vfid
    return _queue_read(dev_fd, file_name, bdev_offset, addr, size)


def read_file_batch(dev_fd: int, file_name: str, bdev_name: str, requests,
                    devid: int = 0, vfid: int = 0) -> int:
    """Queue a batch of ``(bdev_offset, addr, size)`` requests."""
    del bdev_name, devid, vfid
    if not _valid_fd(dev_fd):
        return -errno.EBADF
    if not isinstance(requests, (list, tuple)) or not requests:
        return -errno.EINVAL

    queued_before = len(_pending[dev_fd])
    for request in requests:
        if not isinstance(request, (list, tuple)) or len(request) < 3:
            del _pending[dev_fd][queued_before:]
            return -errno.EINVAL
        result = _queue_read(dev_fd, file_name, request[0], request[1], request[2])
        if result < 0:
            del _pending[dev_fd][queued_before:]
            return result
    return 0


def drain_read(dev_fd: int) -> int:
    """Complete all queued reads for a handle."""
    if not _valid_fd(dev_fd):
        return -errno.EBADF
    for addr, payload in _pending[dev_fd]:
        _buffers[addr] = payload
    _pending[dev_fd].clear()
    return 0


def get_buffer(addr: int) -> Optional[bytes]:
    """Return bytes in mock device memory at ``addr``; unavailable on hardware."""
    return _buffers.get(addr)
