"""ai-switch public command and configuration API."""

from ._version import VERSION
from .config import APPS, MODES, SwitchError, json_bytes, redacted
from .manager import Manager


def parser():
    from .cli import parser as build_parser

    return build_parser()


def main(argv=None):
    from .cli import main as run

    return run(argv)
