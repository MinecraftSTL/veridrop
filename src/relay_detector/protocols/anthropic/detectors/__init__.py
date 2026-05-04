"""Detector registry.

Each new detector should be added to `build_all()` so the Runner picks it up
automatically. Detectors not yet implemented for a given milestone are simply
absent — the Runner tolerates a mode set that references missing names.
"""

from .base import ActiveDetector, BaseDetector, PassiveDetector
from .behavioral_signature import BehavioralSignatureDetector
from .consistency import ConsistencyDetector
from .identity import IdentityDetector
from .integrity import IntegrityDetector
from .knowledge import KnowledgeDetector
from .message_id import MessageIDDetector
from .pdf import PDFDetector
from .protocol import ProtocolDetector
from .structured_output import StructuredOutputDetector
from .thinking_signature import ThinkingSignatureDetector
from .token_usage import TokenUsageDetector


def build_all() -> list[BaseDetector]:
    """Return one fresh instance of every implemented detector."""
    return [
        IdentityDetector(),
        BehavioralSignatureDetector(),
        ThinkingSignatureDetector(),
        ConsistencyDetector(),
        KnowledgeDetector(),
        PDFDetector(),
        StructuredOutputDetector(),
        ProtocolDetector(),
        IntegrityDetector(),
        TokenUsageDetector(),
        MessageIDDetector(),
    ]


__all__ = [
    "BaseDetector",
    "ActiveDetector",
    "PassiveDetector",
    "IdentityDetector",
    "BehavioralSignatureDetector",
    "ThinkingSignatureDetector",
    "ConsistencyDetector",
    "KnowledgeDetector",
    "PDFDetector",
    "StructuredOutputDetector",
    "ProtocolDetector",
    "IntegrityDetector",
    "TokenUsageDetector",
    "MessageIDDetector",
    "build_all",
]
