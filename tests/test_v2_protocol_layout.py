"""Regression tests for the v2 protocol package layout."""

from __future__ import annotations

from importlib import resources

from relay_detector.core.detectors_base import ActiveDetector
from relay_detector.detectors.identity import IdentityDetector as LegacyIdentity
from relay_detector.protocols.anthropic.detectors import build_all
from relay_detector.protocols.anthropic.detectors.identity import (
    IdentityDetector as AnthropicIdentity,
)


def test_legacy_detector_path_points_to_anthropic_implementation():
    assert LegacyIdentity is AnthropicIdentity


def test_anthropic_detectors_inherit_core_base_classes():
    detectors = build_all()
    assert detectors
    assert any(isinstance(detector, ActiveDetector) for detector in detectors)


def test_anthropic_packaged_data_is_available_from_new_resource_path():
    data_root = resources.files("relay_detector.protocols.anthropic.data")
    assert data_root.joinpath("behavioral_signatures.json").is_file()
    assert data_root.joinpath("knowledge_questions.json").is_file()
    assert data_root.joinpath("test_document.pdf").is_file()
