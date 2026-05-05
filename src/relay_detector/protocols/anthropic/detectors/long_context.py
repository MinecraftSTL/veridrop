"""Anthropic long-context truncation detector — needle-in-haystack.

Mirrors the OpenAI implementation but speaks Anthropic Messages API:
  - client.messages_create(...) instead of chat_completions_create
  - max_tokens (not max_completion_tokens)
  - response uses content[].text blocks
  - usage.input_tokens (not prompt_tokens)

For now we DO NOT enable the context-1m beta header, so Opus 4.7's
effective limit stays 200k (matching Sonnet/Haiku's default tier). 1M
testing is planned as a separate opt-in flag with explicit cost preview
($30/run at premium tier pricing) — see docs/long_context_1m.md (TBD).

Opt-in (config.include_long_context). Default: skipped.
"""

from __future__ import annotations

import time

from ....core.long_context import (
    STANDARD_TIERS,
    assemble_haystack,
    build_question,
    estimate_cost_usd,
    evaluate_recalls,
    make_needles,
    model_context_limit,
    tiers_for_model,
)
from ....core.models import DetectorResult
from .base import ActiveDetector

PASS_THRESHOLD = 3
PARTIAL_THRESHOLD = 2

# Anthropic's max_tokens caps the OUTPUT, not the input. 256 is enough for
# the model to recite three IDs comfortably; some Anthropic models burn
# extra tokens on adaptive thinking, so leave headroom.
MAX_OUTPUT_TOKENS = 256


class LongContextDetector(ActiveDetector):
    name = "long_context"
    display_name = "长上下文真实性"
    weight = 15.0  # heavy — context-window fraud is among the worst lies

    async def run(self, client, model: str) -> DetectorResult:
        # Opt-in gate. include_long_context_extreme is the superset (uses
        # adaptive tiers up to model's advertised limit), so it implies
        # the standard one — checking either is enough to enable.
        cfg = self.config
        opt_in_standard = bool(cfg and cfg.include_long_context)
        opt_in_extreme = bool(cfg and cfg.include_long_context_extreme)
        if not (opt_in_standard or opt_in_extreme):
            return self.skip(
                "长上下文检测为可选项,需在请求时勾选(标准档 $0.05–$0.50 / 极限档 $0.05–$8)"
            )

        seed = f"{client.base_url}:{model}:{int(time.time())}"
        tier_results: list[dict] = []
        total_cost_usd = 0.0
        truncation_at: int | None = None
        reached_tier: int | None = None

        ctx_limit = model_context_limit(model)
        # Extreme strategy: adaptive tiers up to ctx_limit (e.g. 32k →
        # 500k → 950k for a 1M model). Standard: hardcoded (32k, 100k,
        # 200k). Extreme wins when both are checked.
        if opt_in_extreme:
            tier_set = tiers_for_model(ctx_limit)
            tier_strategy = "extreme"
        else:
            tier_set = STANDARD_TIERS
            tier_strategy = "standard"

        for target_tokens in tier_set:
            if target_tokens > ctx_limit:
                tier_results.append({
                    "target_tokens": target_tokens,
                    "needles_total": 3,
                    "needles_found": 0,
                    "status": "skip",
                    "skip_reason": (
                        f"模型 {model} 上限为 {ctx_limit} tokens,跳过此档"
                    ),
                    "estimated_cost_usd": 0.0,
                    "input_tokens_reported": None,
                })
                continue

            tier_result = await self._probe_tier(
                client, model, target_tokens, seed, ctx_limit
            )
            tier_results.append(tier_result)
            total_cost_usd += tier_result["estimated_cost_usd"]
            reached_tier = target_tokens
            if tier_result["status"] in ("fail", "partial"):
                last_pass = next(
                    (
                        t["target_tokens"]
                        for t in reversed(tier_results[:-1])
                        if t["status"] == "pass"
                    ),
                    None,
                )
                if last_pass is None:
                    truncation_at = target_tokens // 2
                else:
                    truncation_at = (last_pass + target_tokens) // 2
                break

        score, status, summary = _aggregate(tier_results)

        return self._result(
            status,
            score,
            {
                "summary": summary,
                "tier_strategy": tier_strategy,
                "tiers_tested": tier_results,
                "highest_tier_reached": reached_tier,
                "truncation_inferred_at_tokens": truncation_at,
                "estimated_cost_usd": round(total_cost_usd, 4),
                "model": model,
                "model_context_limit": ctx_limit,
                "opt_in": True,
            },
        )

    async def _probe_tier(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
        ctx_limit: int,
    ) -> dict:
        QUESTION_BUFFER = 500
        tier_seed = f"{seed}:{target_tokens}"
        needles = make_needles(tier_seed)
        haystack_target = min(
            target_tokens - QUESTION_BUFFER,
            ctx_limit - QUESTION_BUFFER,
        )
        haystack = assemble_haystack(haystack_target, needles, tier_seed)
        question = build_question(needles)
        full_prompt = haystack + question

        cost = estimate_cost_usd(target_tokens, model)
        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": full_prompt}],
            )
        except Exception as e:  # noqa: BLE001
            # 413 / context-too-long / timeout etc. — treat as truncation
            # evidence (relay couldn't deliver the advertised window).
            return {
                "target_tokens": target_tokens,
                "needles_total": len(needles),
                "needles_found": 0,
                "status": "fail",
                "error": str(e)[:300],
                "estimated_cost_usd": 0.0,
                "input_tokens_reported": None,
                "response_text_preview": None,
            }

        text = _join_text(resp.get("content"))
        recalls = evaluate_recalls(text, needles)
        found = sum(recalls)
        usage = resp.get("usage") or {}
        input_tokens = usage.get("input_tokens")

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
            "input_tokens_reported": input_tokens,
            "response_text_preview": text[:400],
        }


def _aggregate(tier_results: list[dict]) -> tuple[float, str, str]:
    """Same scoring philosophy as the OpenAI variant — drop skip tiers
    from the average so model-limit constraints don't penalize the relay.
    See protocols/openai/detectors/long_context.py:_aggregate for the full
    rationale."""
    if not tier_results:
        return 0.0, "error", "未跑任何 tier"

    probed = [t for t in tier_results if t["status"] != "skip"]
    if not probed:
        return 0.0, "skip", "模型自身 context 上限低于检测最低档 (32k),跳过"

    per_tier_pct = []
    for t in probed:
        if t["status"] == "pass":
            per_tier_pct.append(100.0)
        elif t["status"] == "partial":
            per_tier_pct.append(66.0)
        else:
            per_tier_pct.append(0.0)
    score = sum(per_tier_pct) / len(per_tier_pct)

    has_fail = any(t["status"] == "fail" for t in probed)
    has_partial = any(t["status"] == "partial" for t in probed)
    skip_count = len(tier_results) - len(probed)

    if has_fail or has_partial:
        bad = next(
            t for t in probed if t["status"] in ("fail", "partial")
        )
        status = "fail"
        if has_fail:
            summary = (
                f"{bad['target_tokens'] // 1000}k tokens 处召回失败 "
                f"({bad['needles_found']}/{bad['needles_total']} needles) "
                "—— 中转站很可能在此规模截断或路由到小窗口模型"
            )
        else:
            summary = (
                f"{bad['target_tokens'] // 1000}k tokens 处仅召回 "
                f"{bad['needles_found']}/{bad['needles_total']} needles "
                "—— 可能存在轻度截断或上下文压缩"
            )
    else:
        highest = probed[-1]["target_tokens"] // 1000
        status = "pass"
        if skip_count > 0:
            summary = (
                f"完整通过 {highest}k tokens 长上下文检测;更高档因模型自身 "
                "上限未测"
            )
        else:
            summary = f"完整通过 {highest}k tokens 长上下文检测,未发现截断证据"

    return score, status, summary


def _join_text(content) -> str:
    """Concat all text blocks from an Anthropic Messages content array."""
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)
