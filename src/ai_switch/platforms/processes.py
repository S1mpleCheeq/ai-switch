"""Public session-lock interface; dispatch without importing foreign OS code."""

import sys
from .errors import TakeoverError, Writer


def _backend():
    if sys.platform == "linux":
        from . import linux

        return linux
    from . import portable

    return portable


def writer(codex_home, session):
    return _backend().writer(codex_home, session)


def ensure_available(
    codex_home, session, *, takeover=False, dry_run=False, timeout=15.0, emit=print
):
    return _backend().ensure_available(
        codex_home,
        session,
        takeover=takeover,
        dry_run=dry_run,
        timeout=timeout,
        emit=emit,
    )
