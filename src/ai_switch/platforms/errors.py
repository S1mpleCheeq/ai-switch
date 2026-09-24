"""Shared platform errors and data structures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PlatformError(OSError):
    """Raised for platform-specific filesystem or permission errors."""

    pass


class TakeoverError(Exception):
    """Raised for session lock ownership and takeover violations."""

    pass


@dataclass(frozen=True)
class Writer:
    path: Path
    key: tuple
    pid: int | None
