"""Observed target capabilities: what a target advertises, not what it declares.

Only the models are re-exported here. ``resolve`` imports ``models.core`` (it
needs a ``TargetDefinition`` and a ``Scenario``), and ``models.core`` embeds a
``ResolvedTargetCapabilities`` in every run result — so re-exporting the
resolver from this package would close an import cycle. Import it explicitly:

    from interop_lab.capabilities.resolve import CapabilityResolver
"""

from interop_lab.capabilities.model import (
    CAPABILITY_LIMITATIONS,
    UNUSABLE_RESOLUTION,
    CapabilityFingerprint,
    CapabilityResolutionStatus,
    ResolvedTargetCapabilities,
    ToolDescriptor,
)

__all__ = [
    "CAPABILITY_LIMITATIONS",
    "UNUSABLE_RESOLUTION",
    "CapabilityFingerprint",
    "CapabilityResolutionStatus",
    "ResolvedTargetCapabilities",
    "ToolDescriptor",
]
