"""Compatibility shim for Anthropic BehavioralSignatureDetector."""

from relay_detector.protocols.anthropic.detectors.behavioral_signature import *  # noqa: F403,E501
from relay_detector.protocols.anthropic.detectors.behavioral_signature import _evaluate  # noqa: E501,F401
