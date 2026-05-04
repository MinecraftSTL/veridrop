"""Compatibility shim for Anthropic baseline comparison."""

from .protocols.anthropic.comparator import *  # noqa: F403
from .protocols.anthropic.comparator import _compare_one  # noqa: F401
