"""Platform primitives; OS-specific backends are loaded only when selected."""

from .errors import PlatformError, TakeoverError, Writer

__all__ = ["PlatformError", "TakeoverError", "Writer"]
