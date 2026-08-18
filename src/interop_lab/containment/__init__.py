"""Execution containment for untrusted targets."""

from interop_lab.containment.imagesource import (
    ImageSource,
    ImageSourceKind,
    LocalBuildImageSource,
    RegistryImageSource,
)
from interop_lab.containment.model import (
    CONTAINMENT_LIMITS,
    UNUSABLE_CONTAINMENT,
    ContainerRuntimeFingerprint,
    ContainerSpec,
    ContainmentOutcome,
    ContainmentRecord,
    ExecutionMode,
    NetworkMode,
    Provenance,
)
from interop_lab.containment.resolution import (
    BuildResolutionEntry,
    BuildResolutionIndex,
    BuildResolutionManifest,
    ImageResolutionError,
    ImageVerificationStatus,
    PortableBuildIdentity,
    ResolvedImage,
)

__all__ = [
    "CONTAINMENT_LIMITS",
    "UNUSABLE_CONTAINMENT",
    "BuildResolutionEntry",
    "BuildResolutionIndex",
    "BuildResolutionManifest",
    "ContainerRuntimeFingerprint",
    "ContainerSpec",
    "ContainmentOutcome",
    "ContainmentRecord",
    "ExecutionMode",
    "ImageResolutionError",
    "ImageSource",
    "ImageSourceKind",
    "ImageVerificationStatus",
    "LocalBuildImageSource",
    "NetworkMode",
    "PortableBuildIdentity",
    "Provenance",
    "RegistryImageSource",
    "ResolvedImage",
]
