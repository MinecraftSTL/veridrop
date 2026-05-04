"""Protocol package isolation checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_files(path: Path) -> list[Path]:
    return [p for p in path.rglob("*.py") if "__pycache__" not in p.parts]


def test_gemini_protocol_does_not_import_claude_or_openai_protocols():
    gemini_root = ROOT / "src" / "relay_detector" / "protocols" / "gemini"
    forbidden = (
        "protocols.anthropic",
        "protocols.openai",
        "relay_detector.protocols.anthropic",
        "relay_detector.protocols.openai",
    )
    offenders: list[str] = []
    for path in _python_files(gemini_root):
        text = path.read_text(encoding="utf-8")
        if any(token in text for token in forbidden):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_core_does_not_import_protocol_packages():
    core_root = ROOT / "src" / "relay_detector" / "core"
    offenders: list[str] = []
    for path in _python_files(core_root):
        text = path.read_text(encoding="utf-8")
        if "relay_detector.protocols" in text or "from ..protocols" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
