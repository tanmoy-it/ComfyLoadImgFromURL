"""Thread-safe, RAM-only image store.

This module deliberately contains no filesystem access of any kind.  Image
bytes only ever live in the ``_Entry.data`` byte strings below and are dropped
as soon as they are consumed (``pop``), expire (TTL), or are evicted.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from typing import NamedTuple

# Limits.  These are intentionally constants rather than a config file so the
# store has no I/O surface at all.  Edit them here if you need different caps.
MAX_BYTES_PER_IMAGE = 50 * 1024 * 1024      # 50 MiB
MAX_TOTAL_BYTES = 500 * 1024 * 1024         # 500 MiB
TTL_SECONDS = 3600.0                        # one hour

TOKEN_BYTES = 16  # secrets.token_urlsafe(16) -> 128 bits of entropy


class RamStoreError(ValueError):
    """Base class for store errors safe to show to a user."""


class ImageTooLarge(RamStoreError):
    pass


class EmptyImage(RamStoreError):
    pass


class _Entry(NamedTuple):
    data: bytes
    created_at: float  # time.monotonic(), never wall-clock


class RamImageStore:
    """Bounded, expiring token -> bytes map.

    Eviction is oldest-insertion-first.  Entries are removed on use (``pop``),
    on expiry, and when the total-byte cap would be exceeded.  All methods are
    safe to call from multiple ComfyUI execution threads.
    """

    def __init__(
        self,
        max_bytes_per_image: int = MAX_BYTES_PER_IMAGE,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        ttl_seconds: float = TTL_SECONDS,
    ) -> None:
        if max_bytes_per_image <= 0:
            raise ValueError("max_bytes_per_image must be positive")
        if max_total_bytes < max_bytes_per_image:
            raise ValueError("max_total_bytes must be >= max_bytes_per_image")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        self.max_bytes_per_image = int(max_bytes_per_image)
        self.max_total_bytes = int(max_total_bytes)
        self.ttl_seconds = float(ttl_seconds)

        self._lock = threading.Lock()
        self._data: "OrderedDict[str, _Entry]" = OrderedDict()
        self._total = 0

    # -- public API ---------------------------------------------------------

    def put(self, data: bytes) -> str:
        """Store image bytes and return an unguessable token."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("image data must be bytes-like")
        data = bytes(data)
        size = len(data)
        if size == 0:
            raise EmptyImage("The image data is empty.")
        if size > self.max_bytes_per_image:
            raise ImageTooLarge(self._too_large_message(size))

        now = time.monotonic()
        with self._lock:
            self._purge_expired_locked(now)
            while self._data and self._total + size > self.max_total_bytes:
                self._evict_oldest_locked()

            token = secrets.token_urlsafe(TOKEN_BYTES)
            while token in self._data:
                token = secrets.token_urlsafe(TOKEN_BYTES)

            self._data[token] = _Entry(data, now)
            self._total += size
            return token

    def pop(self, token: str) -> bytes | None:
        """Remove and return the bytes for *token* (single use), or None."""
        if not token or not isinstance(token, str):
            return None
        now = time.monotonic()
        with self._lock:
            self._purge_expired_locked(now)
            entry = self._data.pop(token, None)
            if entry is None:
                return None
            self._total -= len(entry.data)
            return entry.data

    def peek(self, token: str) -> bytes | None:
        """Return the bytes for *token* without removing them, or None."""
        if not token or not isinstance(token, str):
            return None
        now = time.monotonic()
        with self._lock:
            self._purge_expired_locked(now)
            entry = self._data.get(token)
            return None if entry is None else entry.data

    def purge(self) -> None:
        """Drop every expired entry."""
        with self._lock:
            self._purge_expired_locked(time.monotonic())

    def clear(self) -> None:
        """Drop every entry immediately."""
        with self._lock:
            self._data.clear()
            self._total = 0

    def stats(self) -> dict[str, int | float]:
        """Small diagnostic snapshot (never exposes image bytes or tokens)."""
        with self._lock:
            self._purge_expired_locked(time.monotonic())
            return {
                "count": len(self._data),
                "total_bytes": self._total,
                "max_bytes_per_image": self.max_bytes_per_image,
                "max_total_bytes": self.max_total_bytes,
                "ttl_seconds": self.ttl_seconds,
            }

    # -- internals ---------------------------------------------------------

    def _too_large_message(self, size: int) -> str:
        return (
            f"Image is too large ({size} bytes); the per-image limit is "
            f"{self.max_bytes_per_image} bytes."
        )

    def _purge_expired_locked(self, now: float) -> None:
        # Insertion order is also expiry order: every TTL is identical.
        expired = []
        for token, entry in self._data.items():
            if now - entry.created_at >= self.ttl_seconds:
                expired.append(token)
            else:
                break
        for token in expired:
            entry = self._data.pop(token)
            self._total -= len(entry.data)

    def _evict_oldest_locked(self) -> None:
        _token, entry = self._data.popitem(last=False)
        self._total -= len(entry.data)


# The process-wide store used by both the upload route and the node.  Because
# ComfyUI executes nodes in the same process as the HTTP server, a token
# created by the route is visible to the node with no serialization step.
RAM_STORE = RamImageStore()
