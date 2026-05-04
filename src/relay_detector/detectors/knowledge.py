"""Compatibility shim for Anthropic KnowledgeDetector."""

from relay_detector.protocols.anthropic.detectors.knowledge import *  # noqa: F403
from relay_detector.protocols.anthropic.detectors.knowledge import (  # noqa: F401
    _applies,
    _grade,
    _parse_numbered_answers,
)
