"""OpenAI long-context truncation detector — needle-in-haystack.

Tests whether the relay actually delivers the model's advertised context
window or silently truncates / routes to a smaller-window model.

Strategy (tiered, stop-on-first-failure):

  Tier 1: 32k tokens   — catches transport-layer truncation (nginx body cap)
  Tier 2: 100k tokens  — catches mid-tier truncation (relay summarizes long inputs)
  Tier 3: 200k tokens  — catches high-end truncation (relay routes 4o → 4o-mini)

Each tier sends a haystack with three needles at 10% / 50% / 90% positions
and asks the model to retrieve them. We stop probing further tiers as
soon as one fails — there's no point spending $0.30 on the 200k probe
when the 32k probe already proved truncation at 16k.

Opt-in (config.include_long_context). Default: skipped.
"""

from __future__ import annotations

import time

from ....core.long_context import (
    assemble_haystack,
    build_question,
    estimate_cost_usd,
    evaluate_recalls,
    make_needles,
)
from ....core.models import DetectorResult
from .base import ActiveDetector


# Tier sizes in input tokens. Stopping after the first failure means even a
# bad relay only burns the cheapest probe(s); a clean relay pays full price
# for all three but at gpt-4o-mini that's still ~$0.50 total.
TIERS_TOKENS = (32_000, 100_000, 200_000)

# Each tier's pass threshold. Real models occasionally miss a needle —
# Anthropic and OpenAI both publish ~99% recall, so 2/3 needles is "minor
# wobble", 1/3 is "definite truncation", 0/3 is "completely cut off".
PASS_THRESHOLD = 3      # all needles → pass
PARTIAL_THRESHOLD = 2   # 2/3 → partial (warn but continue)
FAIL_THRESHOLD = 1      # ≤1 → fail (stop probing higher tiers)

# Headroom for the response so the model can recite three IDs comfortably.
# Reasoning models (o1/o3/gpt-5) burn extra reasoning tokens, so leave a
# generous buffer or finish_reason=length cuts off the answer mid-list.
MAX_OUTPUT_TOKENS = 256


class LongContextDetector(ActiveDetector):
    name = "long_context"
    display_name = "长上下文真实性"
    weight = 15.0  # heavy when it runs — context window is a top-tier promise

    async def run(self, client, model: str) -> DetectorResult:
        # Opt-in gate: respects config.include_long_context. Without this gate,
        # every full-mode detection would burn $0.50 of the user's API key.
        if not self.config or not self.config.include_long_context:
            return self.skip(
                "长上下文检测为可选项,需在请求时勾选(成本约 $0.05–$0.50)"
            )

        seed = f"{client.base_url}:{model}:{int(time.time())}"
        tier_results: list[dict] = []
        total_cost_usd = 0.0
        truncation_at: int | None = None
        reached_tier: int | None = None

        for target_tokens in TIERS_TOKENS:
            tier_result = await self._probe_tier(
                client, model, target_tokens, seed
            )
            tier_results.append(tier_result)
            total_cost_usd += tier_result["estimated_cost_usd"]
            reached_tier = target_tokens
            # Stop on fail OR partial — partial is treated as fail in
            # aggregation, so paying $0.30 for the next tier just to confirm
            # the same conclusion is wasteful.
            if tier_result["status"] in ("fail", "partial"):
                # Estimate where truncation occurred: somewhere between the
                # last tier that passed and this one. If even 32k failed,
                # truncation is below 32k.
                last_pass = next(
                    (
                        t["target_tokens"]
                        for t in reversed(tier_results[:-1])
                        if t["status"] == "pass"
                    ),
                    None,
                )
                if last_pass is None:
                    truncation_at = target_tokens // 2  # rough lower-bound
                else:
                    truncation_at = (last_pass + target_tokens) // 2
                break

        score, status, summary = _aggregate(tier_results)

        return self._result(
            status,
            score,
            {
                "summary": summary,
                "tiers_tested": tier_results,
                "highest_tier_reached": reached_tier,
                "truncation_inferred_at_tokens": truncation_at,
                "estimated_cost_usd": round(total_cost_usd, 4),
                "model": model,
                "opt_in": True,
            },
        )

    async def _probe_tier(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
    ) -> dict:
        """Run one tier: build haystack, send, score recall."""
        tier_seed = f"{seed}:{target_tokens}"
        needles = make_needles(tier_seed)
        haystack = assemble_haystack(target_tokens, needles, tier_seed)
        question = build_question(needles)
        full_prompt = haystack + question

        cost = estimate_cost_usd(target_tokens, model)
        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": full_prompt}],
            )
        except Exception as e:  # noqa: BLE001
            # Network errors (413 too-large, timeouts, etc.) are treated as
            # truncation evidence — the relay refused to handle this size.
            return {
                "target_tokens": target_tokens,
                "needles_total": len(needles),
                "needles_found": 0,
                "status": "fail",
                "error": str(e)[:300],
                "estimated_cost_usd": 0.0,  # not charged when request errors
                "prompt_tokens_reported": None,
                "response_text_preview": None,
            }

        text = _message_text(resp)
        recalls = evaluate_recalls(text, needles)
        found = sum(recalls)
        usage = resp.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")

        if found >= PASS_THRESHOLD:
            tier_status = "pass"
        elif found >= PARTIAL_THRESHOLD:
            tier_status = "partial"
        else:
            tier_status = "fail"

        return {
            "target_tokens": target_tokens,
            "needles_total": len(needles),
            "needles_found": found,
            "needle_recalls": [
                {"label": n.label, "found": r}
                for n, r in zip(needles, recalls)
            ],
            "status": tier_status,
            "estimated_cost_usd": cost,
            "prompt_tokens_reported": prompt_tokens,
            "response_text_preview": text[:400],
        }


def _aggregate(tier_results: list[dict]) -> tuple[float, str, str]:
    """Combine per-tier outcomes into one score / status / human summary.

    Score philosophy:
      - Each tier worth equal portion of the score (33% each)
      - pass=100, partial=66, fail=0 within a tier
      - Skipped tiers (because earlier failed) are NOT penalized — they
        don't subtract from score. We only penalize what we observed.

    Status:
      - All pass → pass
      - Any fail → fail (truncation evidence is binary critical)
      - Any partial without fail → fail (don't be soft; partial recall
        at advertised limits is itself a problem worth flagging)
    """
    if not tier_results:
        return 0.0, "error", "未跑任何 tier"

    per_tier_pct = []
    for t in tier_results:
        if t["status"] == "pass":
            per_tier_pct.append(100.0)
        elif t["status"] == "partial":
            per_tier_pct.append(66.0)
        else:
            per_tier_pct.append(0.0)

    # Untested tiers (because we stopped early) get 0 pct — explicitly
    # acknowledging "we couldn't verify higher tiers because lower failed".
    while len(per_tier_pct) < len(TIERS_TOKENS):
        per_tier_pct.append(0.0)
    score = sum(per_tier_pct) / len(per_tier_pct)

    # Status decision
    has_fail = any(t["status"] == "fail" for t in tier_results)
    has_partial = any(t["status"] == "partial" for t in tier_results)

    if has_fail:
        last = tier_results[-1]
        status = "fail"
        summary = (
            f"{last['target_tokens'] // 1000}k tokens 处召回失败 "
            f"({last['needles_found']}/{last['needles_total']} needles)"
            "—— 中转站很可能在此规模截断或路由到小窗口模型"
        )
    elif has_partial:
        status = "fail"  # partial at advertised limit IS a problem
        summary = "在某档存在 needle 召回不全,可能存在轻度截断或上下文压缩"
    else:
        # All probed tiers passed
        highest = tier_results[-1]["target_tokens"] // 1000
        status = "pass"
        summary = f"完整通过 {highest}k tokens 长上下文检测,未发现截断证据"

    return score, status, summary


def _message_text(resp: dict) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""
