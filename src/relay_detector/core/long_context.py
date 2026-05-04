"""Protocol-agnostic needle-in-haystack primitives for long-context detection.

The long-context detector probes whether a relay actually honors the model's
advertised context window, or silently truncates / routes to a smaller-window
model. Each protocol's detector calls into here to:

  1. Build a multi-needle haystack of approximately N tokens
  2. Send it through the relay (protocol-specific)
  3. Score how many needles the model can recall

Why three needles at 10% / 50% / 90%:
  - A single needle at the end is fooled by relays that pass head + tail and
    drop the middle. Three positions catches sliding-window truncation.
  - Multiple needles also smooth out occasional natural recall failures —
    even genuine 1M-context Claude misses ~1% of needles.

We don't use copyrighted text; the haystack is generated from random
template instantiations so it's deterministic-by-seed yet not in any
public dataset (which would let a model cheat via memorization).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass


# Approximate chars-per-token for English mixed with numbers and punctuation.
# tiktoken's cl100k tokenizer averages ~3.8 for plain English; we use 4.0 to
# slightly overshoot the target, which is the safe direction (better to send
# more than the tier and still detect truncation than fall short).
_CHARS_PER_TOKEN = 4.0


@dataclass
class Needle:
    """One fact embedded in the haystack that the model must recall.

    `position_pct` is where the sentence is inserted in the haystack
    (0.0 = start, 1.0 = end). `answer` is the unique identifier we expect
    to find verbatim in the model's response.
    """
    position_pct: float
    sentence: str       # inserted into haystack at position_pct
    answer: str         # the unique substring the model must reproduce
    label: str          # human-readable "what to search for" hint


def make_needles(seed: str) -> list[Needle]:
    """Three deterministic needles seeded by relay identity.

    Each Veridrop run gets its own seed (relay domain + timestamp), so
    needles are unique per check — a relay that memorized one set won't
    fool the next user. Three positions catch sliding-window truncation
    that a single end-needle would miss.
    """
    rng = random.Random(seed)
    return [
        Needle(
            position_pct=0.10,
            sentence=(
                "Important administrative note: the conference room access "
                "passcode for the duration of this document is {answer}. "
                "All attendees should retain this code for badge verification."
            ).format(answer=(passcode := _random_id(rng, "ALPHA"))),
            answer=passcode,
            label="conference room access passcode",
        ),
        Needle(
            position_pct=0.50,
            sentence=(
                "Editor's note inserted mid-document: the internal project "
                "codename for the upcoming deliverable is {answer}. This "
                "codename supersedes any prior placeholder identifiers."
            ).format(answer=(codename := _random_id(rng, "ATLAS"))),
            answer=codename,
            label="internal project codename",
        ),
        Needle(
            position_pct=0.90,
            sentence=(
                "Final operational note before closing: in case of emergency, "
                "the on-call engineer's reference identifier is {answer}. "
                "This identifier should be quoted in all incident reports."
            ).format(answer=(contact := _random_id(rng, "BRAVO"))),
            answer=contact,
            label="on-call engineer reference identifier",
        ),
    ]


def build_question(needles: list[Needle]) -> str:
    """The retrieval prompt appended after the haystack.

    Uses an explicit "report each on its own line" structure so we can
    parse model output reliably across response styles. Avoids leading
    instructions like "you must find" — those can prime the model to
    fabricate when content is truncated. We want honest "NOT FOUND"
    responses to discriminate truncation from comprehension failure.
    """
    bullets = "\n".join(f"  - The {n.label}" for n in needles)
    return (
        "\n\n---\n"
        "Carefully review the document above and locate the following "
        "identifiers, which were embedded as administrative notes:\n"
        f"{bullets}\n\n"
        "Reply with each identifier on its own line, in the order asked. "
        "If any identifier is not present in the document, write 'NOT FOUND' "
        "for that one. Do not guess or invent identifiers."
    )


def evaluate_recalls(response_text: str, needles: list[Needle]) -> list[bool]:
    """For each needle, did the model reproduce its `answer` in the response?

    Case-insensitive substring match — the answers are unique IDs (e.g.
    'ALPHA-3F8D-2C91') that won't collide with normal text by accident.
    Models sometimes paraphrase or list with different formatting; substring
    match tolerates this without giving false positives.
    """
    if not response_text:
        return [False] * len(needles)
    haystack = response_text.upper()
    return [n.answer.upper() in haystack for n in needles]


def assemble_haystack(target_tokens: int, needles: list[Needle], seed: str) -> str:
    """Build a haystack of approximately target_tokens with needles embedded
    at their target positions.

    The filler text is synthetic — random template instantiations of
    plausible-looking factual sentences (fictional company reports, fake
    research summaries, etc.). This:
      - avoids copyright concerns
      - is deterministic per seed (same input → same haystack, reproducible)
      - is varied enough that simple compressors can't shrink it (~4 ch/tok)
      - is not in any public dataset, so models can't cheat via memorization

    Needles are placed at their position_pct points by inserting them
    between filler sentences. We don't slice mid-sentence to keep the
    document readable — model recall is sharper on coherent context.
    """
    target_chars = int(target_tokens * _CHARS_PER_TOKEN)
    rng = random.Random(seed + ":haystack")

    sentences: list[str] = []
    char_count = 0
    while char_count < target_chars:
        s = _filler_sentence(rng)
        sentences.append(s)
        char_count += len(s) + 1  # +1 for joining space

    # Insert needles at target positions. We sort by position desc so
    # earlier insertions don't shift later indices.
    needle_inserts = sorted(
        [(int(n.position_pct * len(sentences)), n.sentence) for n in needles],
        key=lambda x: x[0],
        reverse=True,
    )
    for idx, sentence in needle_inserts:
        sentences.insert(idx, sentence)

    return " ".join(sentences)


def estimate_cost_usd(target_tokens: int, model: str) -> float:
    """Rough USD estimate for sending target_tokens of input.

    Hard-coded per-model price tiers — accurate enough for "is this $0.05 or
    $5?" guidance shown in the report's details. Real billing depends on
    provider tier transitions (e.g. Anthropic's >200k surcharge) which this
    helper does NOT model — only the base input rate. Conservative for users.
    """
    # USD per 1M input tokens, base tier
    rates = {
        # OpenAI
        "gpt-4o-mini":      0.15,
        "gpt-4o":           2.50,
        "gpt-4.1-mini":     0.40,
        "gpt-4.1":          2.00,
        "gpt-5-mini":       0.25,
        "gpt-5":            2.50,
        "gpt-3.5-turbo":    0.50,
        # Anthropic
        "claude-haiku-4-5":  1.00,
        "claude-sonnet-4-6": 3.00,
        "claude-opus-4-7":  15.00,
        "claude-opus-4-6":  15.00,
        # Gemini
        "gemini-2.5-flash": 0.075,
        "gemini-2.5-pro":   1.25,
    }
    rate = rates.get(model)
    if rate is None:
        for prefix, r in rates.items():
            if model.startswith(prefix):
                rate = r
                break
        else:
            rate = 1.0  # fallback assumption
    return round(target_tokens * rate / 1_000_000, 4)


# ---------- Internal helpers ----------


def _random_id(rng: random.Random, prefix: str) -> str:
    """Generate a unique identifier like ALPHA-3F8D-2C91 — short enough to
    fit naturally in a sentence, long enough to never collide with normal
    text. Hex-only after prefix so the model can't "fix" it via spell-check."""
    return f"{prefix}-{rng.randrange(0xFFFF):04X}-{rng.randrange(0xFFFF):04X}"


_VOCAB = {
    "company": [
        "Acme Industries", "Blackstar Logistics", "Cardinal Robotics",
        "Delphi Technologies", "Evergreen Pharmaceuticals", "Falcon Aerospace",
        "Granite Holdings", "Halcyon Capital", "Iron Peak Mining",
        "Juno Biosciences", "Kestrel Manufacturing", "Lighthouse Energy",
        "Meridian Foods", "Northgate Construction", "Olympia Software",
    ],
    "department": [
        "finance", "operations", "research and development", "human resources",
        "legal", "marketing", "supply chain", "engineering", "compliance",
    ],
    "metric": [
        "operating revenue", "gross margin", "year-over-year growth",
        "customer acquisition cost", "throughput rate", "cycle time",
        "defect density", "utilization", "satisfaction score",
    ],
    "city": [
        "Geneva", "Singapore", "Buenos Aires", "Reykjavík", "Cape Town",
        "Vancouver", "Tashkent", "Auckland", "Edinburgh", "Marrakech",
        "Stockholm", "Hanoi", "Lima", "Tbilisi", "Kuala Lumpur",
    ],
    "topic": [
        "supply chain resilience", "regulatory compliance", "energy efficiency",
        "talent retention", "data governance", "risk management",
        "operational continuity", "quality assurance", "stakeholder engagement",
    ],
    "verb": [
        "outlined", "evaluated", "presented", "documented", "summarized",
        "introduced", "compared", "analyzed", "described", "highlighted",
    ],
}


def _filler_sentence(rng: random.Random) -> str:
    """One synthetic sentence ~80–180 chars. Mix of templates for variety
    (~20–45 tokens each) so the haystack converges on the target length
    in 500–10000 sentences depending on tier."""
    template = rng.choice([
        "In the {quarter} quarter of fiscal year {year}, the {department} "
        "department of {company} {verb} a {metric} review covering {topic}, "
        "with findings circulated to leadership for ratification.",

        "At the {year} industry symposium held in {city}, delegates {verb} "
        "the role of {topic} in shaping medium-term policy, citing data "
        "from the {company} compliance archive and the {department} working group.",

        "The internal {department} memorandum dated {month} {year} {verb} "
        "revisions to {company}'s {topic} guidelines, intended to align "
        "with the upcoming {metric} reporting cycle.",

        "Researchers at {company} {verb} the relationship between {topic} "
        "and {metric}, documenting interim results in a working paper "
        "circulated within the {department} unit during {quarter} of {year}.",

        "A cross-functional task force consisting of {department} and "
        "operations staff at {company} {verb} the {year} {topic} report, "
        "noting that the {metric} trajectory diverged from earlier projections.",
    ])
    return template.format(
        quarter=rng.choice(["first", "second", "third", "fourth"]),
        year=rng.randrange(2014, 2027),
        company=rng.choice(_VOCAB["company"]),
        department=rng.choice(_VOCAB["department"]),
        metric=rng.choice(_VOCAB["metric"]),
        city=rng.choice(_VOCAB["city"]),
        topic=rng.choice(_VOCAB["topic"]),
        verb=rng.choice(_VOCAB["verb"]),
        month=rng.choice([
            "January", "March", "May", "July", "September", "November",
        ]),
    )


# Public for tests
ANSWER_RE = re.compile(r"\b[A-Z]+-[0-9A-F]{4}-[0-9A-F]{4}\b")
