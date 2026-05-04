"""OpenAI Chat Completions detector registry."""

from .base import ActiveDetector, BaseDetector, PassiveDetector
from .basic_request import BasicRequestDetector
from .function_calling import FunctionCallingDetector
from .integrity import IntegrityDetector
from .model_consistency import ModelConsistencyDetector
from .protocol import ProtocolDetector
from .structured_output import StructuredOutputDetector
from .token_billing import TokenBillingDetector


def build_all() -> list[BaseDetector]:
    return [
        BasicRequestDetector(),
        ModelConsistencyDetector(),
        FunctionCallingDetector(),
        StructuredOutputDetector(),
        ProtocolDetector(),
        IntegrityDetector(),
        TokenBillingDetector(),
    ]


__all__ = [
    "BaseDetector",
    "ActiveDetector",
    "PassiveDetector",
    "BasicRequestDetector",
    "ModelConsistencyDetector",
    "FunctionCallingDetector",
    "StructuredOutputDetector",
    "ProtocolDetector",
    "IntegrityDetector",
    "TokenBillingDetector",
    "build_all",
]
