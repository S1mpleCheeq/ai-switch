"""Run tests against the checkout or an explicitly installed package."""

import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src"
if os.environ.get("AI_SWITCH_TEST_INSTALLED") == "1":
    # unittest discovery adds the checkout to sys.path. Exclude its legacy
    # bootstrap while importing the installed distribution, then restore paths
    # so tests can still inspect release.py and the source archive manifest.
    original = sys.path[:]
    sys.path[:] = [
        p for p in sys.path if Path(p or os.getcwd()).resolve() not in (REPO, SOURCE)
    ]
    try:
        import ai_switch

        location = Path(ai_switch.__file__).resolve()
        assert SOURCE not in location.parents and location != REPO / "ai_switch.py", (
            location
        )
    finally:
        sys.path[:] = original
    paths = [
        p
        for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if p and Path(p).resolve() not in (REPO, SOURCE)
    ]
    if paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        os.environ.pop("PYTHONPATH", None)
else:
    sys.path.insert(0, str(SOURCE))
    paths = [
        str(SOURCE),
        *[
            p
            for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
            if p and Path(p).resolve() != SOURCE
        ],
    ]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
