#!/usr/bin/env python3
VERSION = "1.10.0"
"""Compatibility entry for source checkouts; installed commands use the package."""
from pathlib import Path
import sys

_source = Path(__file__).resolve().parent / "src"
if __name__ == "__main__":
    sys.path.insert(0, str(_source))
    from ai_switch.cli import main

    raise SystemExit(main())
else:
    # Keep historical source-tree imports usable without copying package logic.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        __name__,
        _source / "ai_switch" / "__init__.py",
        submodule_search_locations=[str(_source / "ai_switch")],
    )
    _package = importlib.util.module_from_spec(_spec)
    sys.modules[__name__] = _package
    _spec.loader.exec_module(_package)
