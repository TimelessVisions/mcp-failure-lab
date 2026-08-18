"""Severity, in its own module so two models can share it without a cycle.

``models.core`` defines the primary result vocabulary and ``models.dimensions``
defines the secondary one; both need severity, and core imports dimensions. A
shared leaf module is the smallest thing that lets that hold. ``Severity`` is
still re-exported from ``models.core``, which is where every existing caller
imports it from.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
