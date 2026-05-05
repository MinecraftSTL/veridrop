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
        # Opt-in gate. `include_long_context_extreme` is the superset
        # (uses adaptive tiers up to model's advertised limit), so it
        # implies the standard one — checking either is enough to enable.
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
        # Extreme strategy: adaptive tiers up to ctx_limit. Standard:
        # hardcoded (32k, 100k, 200k). Extreme wins when both are checked.
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
                    "prompt_tokens_reported": None,
                })
                continue

            tier_result = await self._probe_tier(
                client, model, target_tokens, seed, ctx_limit
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
        """Run one tier: build haystack, send, score recall.

        Haystack is sized to leave ~QUESTION_BUFFER tokens of headroom for the
        question itself + a small response. For tiers at the model's exact
        context limit (e.g. 200k tier on a 200k model), the haystack is
        clamped to ctx_limit - buffer so the request actually fits.
        """
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

    Three kinds of tier entries can appear in tier_results:
      - probed (pass/partial/fail): tested with a real request
      - skip: not probed because target exceeds model's own context window
      - (loop break): tiers after a fail aren't in tier_results at all

    Scoring drops skip entries — they reflect a model limit, not a relay
    flaw, so penalizing for them would conflate two different problems.
    Tiers untested because of an earlier failure are also dropped from the
    denominator: the failed tier's 0% already represents the verdict, and
    averaging two 0% tiers would just halve the score for the same evidence.
    """
    if not tier_results:
        return 0.0, "error", "未跑任何 tier"

    probed = [t for t in tier_results if t["status"] != "skip"]
    if not probed:
        # Model's own context limit is below all our probe tiers — we
        # couldn't test anything. Caller will have already reported this
        # via the skip tier entries.
        return 0.0, "skip", "模型自身 context 上限低于检测最低档 (32k),跳过"

    per_tier_pct = []
    for t in probed:
        if t["status"] == "pass":
            per_tier_pct.append(100.0)
        elif t["status"] == "partial":
            per_tier_pct.append(66.0)
        else:  # fail
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
        # All probed tiers passed. Highest probed tier reached.
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


def _message_text(resp: dict) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""
