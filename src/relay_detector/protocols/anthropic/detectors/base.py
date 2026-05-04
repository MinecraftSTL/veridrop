"""Compatibility re-export for Anthropic detectors.

All detector base classes live in relay_detector.core.detectors_base so the
shared runner can use isinstance() across protocol packages.
"""

from ....core.detectors_base import ActiveDetector, BaseDetector, PassiveDetector

__all__ = ["ActiveDetector", "BaseDetector", "PassiveDetector"]
