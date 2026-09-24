"""Shared service context; concrete services own their operations."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import Manager


class Service:
    def __init__(self, context: Manager):
        self.ctx = context
