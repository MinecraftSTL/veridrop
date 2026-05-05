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

import asyncio
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


def _tier_timeout_s(target_tokens: int) -> float:
    """Per-tier HTTP timeout — see OpenAI variant for full rationale.
    Same scaling: 120s floor, +1s per 4k tokens. 950k → ~240s."""
    return max(120.0, target_tokens / 4_000.0)


_RATE_LIMIT_MARKERS = (
    "http 429",
    "rate limit",
    "rate_limit_exceeded",
    "tokens per min",
    "tpm",
    "requests per min",
    "overloaded_error",  # Anthropic's transient overload signal
)


def _looks_rate_limited(err_msg: str) -> bool:
    """See OpenAI long_context variant — same logic, same markers plus
    Anthropic's `overloaded_error` which is also transient and not a
    truncation signal."""
    if not err_msg:
        return False
    lower = err_msg.lower()
    return any(m in lower for m in _RATE_LIMIT_MARKERS)


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

            tier_result = await self._probe_tier_with_tpm_retry(
                client, model, target_tokens, seed, ctx_limit
            )
            tier_results.append(tier_result)
            total_cost_usd += tier_result["estimated_cost_usd"]
            reached_tier = target_tokens
            # Stop on rate_limited too — TPM windows reset on the order
            # of minutes, retrying the next tier within this run hits
            # the same wall.
            if tier_result["status"] == "rate_limited":
                break
            if tier_result["status"] == "fail":
                # Strong truncation evidence (0-1/3 needles found) — infer
                # where the relay caps. Partial tiers (2/3) are NOT
                # truncation evidence: Claude's known lost-in-the-middle
                # behavior at the 200k tier ceiling can produce 2/3 even
                # against api.anthropic.com directly.
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
            if tier_result["status"] == "partial":
                # Stop probing higher tiers (likely same wobble), but
                # don't claim truncation.
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

    async def _precount_input_tokens(
        self, client, model: str, prompt: str
    ) -> int | None:
        """Ask Anthropic exactly how many input_tokens the request will be,
        without sending it.

        Returns None on any failure (relay doesn't implement the endpoint,
        rate-limited, network error). Caller falls back to its chars/token
        estimate in that case — better to proceed with a slight overshoot
        risk than to fail the whole tier on a count_tokens hiccup.
        """
        try:
            _req, resp, _h, _lat = await client.count_tokens(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001 — best-effort verification only
            return None
        n = resp.get("input_tokens") if isinstance(resp, dict) else None
        return n if isinstance(n, int) else None

    async def _probe_tier_with_tpm_retry(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
        ctx_limit: int,
    ) -> dict:
        """Sleep ~75s and retry once on rate_limited — see OpenAI variant
        for full rationale. Anthropic's RPM window is also 60s sliding,
        so the same wait-out strategy applies."""
        result = await self._probe_tier(
            client, model, target_tokens, seed, ctx_limit
        )
        if result["status"] != "rate_limited":
            return result

        wait_s = 75.0
        await asyncio.sleep(wait_s)
        retry = await self._probe_tier(
            client, model, target_tokens, seed, ctx_limit
        )
        retry["tpm_retry_attempted"] = True
        retry["tpm_retry_wait_s"] = wait_s
        retry["estimated_cost_usd"] += result.get("estimated_cost_usd", 0.0)
        return retry

    async def _probe_tier(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
        ctx_limit: int,
    ) -> dict:
        # Use chars/tok estimation only as the FIRST guess. The real source
        # of truth is Anthropic's /v1/messages/count_tokens endpoint — we
        # call it before sending to know exactly how big the request will
        # be, then trim if it would exceed ctx_limit.
        QUESTION_BUFFER = 1500
        tier_seed = f"{seed}:{target_tokens}"
        needles = make_needles(tier_seed)
        haystack_target = min(
            target_tokens - QUESTION_BUFFER,
            ctx_limit - QUESTION_BUFFER,
        )
        haystack = assemble_haystack(
            haystack_target, needles, tier_seed, protocol="anthropic",
        )
        question = build_question(needles)
        full_prompt = haystack + question

        # Verify exact input_tokens via count_tokens API. This is Anthropic's
        # canonical way to predict token cost without sending — accurate to
        # the token. If the relay doesn't support this endpoint, we silently
        # fall through to send with our chars/tok estimate.
        precounted = await self._precount_input_tokens(client, model, full_prompt)
        if precounted is not None and precounted > ctx_limit - 500:
            # Trim: compute actual chars/token and rebuild haystack to fit.
            # The 0.97 factor is 3% extra margin in case the rebuild lands
            # slightly larger than predicted (Anthropic's own count is
            # deterministic per model+content though, so margin is small).
            actual_chars_per_tok = len(full_prompt) / max(precounted, 1)
            target_total_chars = (ctx_limit - 500) * actual_chars_per_tok * 0.97
            new_haystack_chars = max(0, target_total_chars - len(question))
            new_haystack_tokens = max(
                1000, int(new_haystack_chars / actual_chars_per_tok)
            )
            haystack = assemble_haystack(
                new_haystack_tokens, needles, tier_seed, protocol="anthropic",
            )
            full_prompt = haystack + question

        cost = estimate_cost_usd(target_tokens, model)
        timeout = _tier_timeout_s(target_tokens)
        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": full_prompt}],
                request_timeout_s=timeout,
            )
        except Exception as e:  # noqa: BLE001
            # Pull raw body when it's an AnthropicAPIError — its str() is
            # capped at 200 chars which crops important diagnostic detail
            # like "Requested X, please try again in Y".
            body = getattr(e, "body", None)
            err_msg = (
                f"HTTP {getattr(e, 'status', '?')}: {body}"
                if isinstance(body, str) else str(e)
            )
            # Distinguish rate-limit (provider's TPM/RPM cap, not
            # truncation) from real failures (413 / context-too-long /
            # timeout). The first should NOT count against the relay.
            if _looks_rate_limited(err_msg):
                return {
                    "target_tokens": target_tokens,
                    "needles_total": len(needles),
                    "needles_found": 0,
                    "status": "rate_limited",
                    "error": err_msg[:1500],
                    "estimated_cost_usd": 0.0,
                    "input_tokens_reported": None,
                    "response_text_preview": None,
                }
            return {
                "target_tokens": target_tokens,
                "needles_total": len(needles),
                "needles_found": 0,
                "status": "fail",
                "error": err_msg[:1500],
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

    inconclusive = {"skip", "rate_limited"}
    probed = [t for t in tier_results if t["status"] not in inconclusive]
    rate_limited = [t for t in tier_results if t["status"] == "rate_limited"]

    if not probed:
        if rate_limited:
            t = rate_limited[0]
            return 0.0, "skip", (
                f"{t['target_tokens'] // 1000}k tokens probe 触发上游 "
                "rate limit (TPM/RPM),非中转站缺陷 —— 请稍后重试或换更高 tier 的 key"
            )
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
    skip_count = sum(1 for t in tier_results if t["status"] == "skip")

    # has_fail = 0-1/3 needles = strong truncation signal → status=fail
    # has_partial = 2/3 needles, no fail = soft signal. Don't fail outright:
    #   Claude (and OpenAI) both have known lost-in-the-middle behavior at
    #   the limit of advertised context — a 50% needle missed at 200k tier
    #   on real claude-haiku-4-5 has been observed in our own live tests
    #   against api.anthropic.com (the truth source). Calling that "fail"
    #   makes the detector reject the ground-truth model. Score still
    #   reflects 2/3 (66%) so the summary surfaces the wobble, but the
    #   tier verdict is pass — the signal isn't strong enough for fail.
    if has_fail:
        bad = next(t for t in probed if t["status"] == "fail")
        status = "fail"
        summary = (
            f"{bad['target_tokens'] // 1000}k tokens 处召回失败 "
            f"({bad['needles_found']}/{bad['needles_total']} needles) "
            "—— 中转站很可能在此规模截断或路由到小窗口模型"
        )
    else:
        highest = probed[-1]["target_tokens"] // 1000
        status = "pass"
        suffix_parts = []
        if has_partial:
            partial = next(t for t in probed if t["status"] == "partial")
            suffix_parts.append(
                f"{partial['target_tokens'] // 1000}k 档召回 "
                f"{partial['needles_found']}/{partial['needles_total']}"
                "(模型在长上下文中段位置的自然召回缺失,非截断)"
            )
        if skip_count > 0:
            suffix_parts.append("更高档因模型自身上限未测")
        if rate_limited:
            rl = rate_limited[0]
            suffix_parts.append(
                f"{rl['target_tokens'] // 1000}k 档触发上游 TPM 限制(非截断,未计分)"
            )
        if suffix_parts:
            summary = f"完整通过 {highest}k tokens 长上下文检测;" + ";".join(suffix_parts)
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
