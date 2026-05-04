"""Compatibility shim for the Anthropic client."""

from .protocols.anthropic.client import *  # noqa: F403
from .protocols.anthropic.client import _normalize_base_url, _parse_sse  # noqa: F401
