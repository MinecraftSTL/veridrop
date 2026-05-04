"""OpenAI response protocol templates.

These validators are the first layer for OpenAI-compatible relay testing:
they check whether a raw response looks like the official wire shape before
we compare it against an official baseline.

The checks are deliberately structural. They do not try to prove model
authenticity; they catch common relay fingerprints such as Claude-style
payloads, missing usage, wrong IDs, malformed tool calls, and streaming/non-
streaming adapters that return a plausible answer inside the wrong envelope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


WireAPI = Literal["responses", "chat_completions"]
Severity = Literal["critical", "major", "minor"]


@dataclass(frozen=True)
class ProtocolTemplate:
    """A compact description of an official OpenAI response envelope."""

    name: str
    wire_api: WireAPI
    endpoint: str
    response_object: str
    id_prefix: str
    required_top_level: tuple[str, ...]
    usage_required: tuple[str, ...]
    allowed_statuses: tuple[str, ...] = ()
    allowed_finish_reasons: tuple[str | None, ...] = ()
    allowed_output_item_types: tuple[str, ...] = ()
    allowed_content_item_types: tuple[str, ...] = ()


RESPONSES_TEMPLATE = ProtocolTemplate(
    name="OpenAI Responses API response",
    wire_api="responses",
    endpoint="/v1/responses",
    response_object="response",
    id_prefix="resp_",
    required_top_level=(
        "id",
        "object",
        "created_at",
        "status",
        "model",
        "output",
        "usage",
    ),
    usage_required=("input_tokens", "output_tokens", "total_tokens"),
    allowed_statuses=(
        "completed",
        "failed",
        "in_progress",
        "cancelled",
        "incomplete",
        "queued",
    ),
    allowed_output_item_types=(
        "message",
        "function_call",
        "reasoning",
        "web_search_call",
        "file_search_call",
        "computer_call",
        "code_interpreter_call",
        "image_generation_call",
        "mcp_call",
    ),
    allowed_content_item_types=(
        "output_text",
        "refusal",
        "output_image",
        "input_text",
        "input_image",
        "input_file",
    ),
)


CHAT_COMPLETIONS_TEMPLATE = ProtocolTemplate(
    name="OpenAI Chat Completions response",
    wire_api="chat_completions",
    endpoint="/v1/chat/completions",
    response_object="chat.completion",
    id_prefix="chatcmpl-",
    required_top_level=(
        "id",
        "object",
        "created",
        "model",
        "choices",
        "usage",
    ),
    usage_required=("prompt_tokens", "completion_tokens", "total_tokens"),
    allowed_finish_reasons=(
        "stop",
        "length",
        "tool_calls",
        "content_filter",
        "function_call",
        None,
    ),
)


@dataclass
class ProtocolIssue:
    severity: Severity
    code: str
    path: str
    message: str
    expected: Any | None = None
    actual: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class TemplateValidation:
    wire_api: WireAPI
    template_name: str
    score: float
    issues: list[ProtocolIssue] = field(default_factory=list)
    fingerprints: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.score >= 80.0 and not any(
            issue.severity == "critical" for issue in self.issues
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "wire_api": self.wire_api,
            "template_name": self.template_name,
            "score": self.score,
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "fingerprints": self.fingerprints,
        }


def validate_openai_payload(
    wire_api: WireAPI,
    payload: dict[str, Any],
    request_model: str | None = None,
) -> TemplateValidation:
    """Validate a raw OpenAI response payload against a wire API template."""

    if wire_api == "responses":
        return validate_responses_api(payload, request_model=request_model)
    if wire_api == "chat_completions":
        return validate_chat_completion(payload, request_model=request_model)
    raise ValueError(f"unknown OpenAI wire API: {wire_api!r}")


def validate_responses_api(
    payload: dict[str, Any],
    request_model: str | None = None,
) -> TemplateValidation:
    issues: list[ProtocolIssue] = []
    t = RESPONSES_TEMPLATE

    _check_top_level(payload, t, issues)
    _check_id(payload.get("id"), t.id_prefix, "$.id", issues)
    _check_eq(payload.get("object"), t.response_object, "$.object", issues)
    _check_nonneg_int(payload.get("created_at"), "$.created_at", issues)

    status = payload.get("status")
    if status not in t.allowed_statuses:
        issues.append(
            ProtocolIssue(
                "major",
                "status_invalid",
                "$.status",
                "Responses API status must be an official enum value",
                expected=t.allowed_statuses,
                actual=status,
            )
        )

    _check_model(payload.get("model"), request_model, "$.model", issues)
    _check_usage(payload.get("usage"), t.usage_required, "$.usage", issues)

    output = payload.get("output")
    if not isinstance(output, list):
        issues.append(
            ProtocolIssue(
                "critical",
                "output_not_array",
                "$.output",
                "Responses API output must be a list of output items",
                expected="array",
                actual=type(output).__name__,
            )
        )
    else:
        for i, item in enumerate(output):
            _check_response_output_item(item, i, t, issues)

    fingerprints = _responses_fingerprints(payload)
    return TemplateValidation(
        wire_api=t.wire_api,
        template_name=t.name,
        score=_score(issues),
        issues=issues,
        fingerprints=fingerprints,
    )


def validate_chat_completion(
    payload: dict[str, Any],
    request_model: str | None = None,
) -> TemplateValidation:
    issues: list[ProtocolIssue] = []
    t = CHAT_COMPLETIONS_TEMPLATE

    _check_top_level(payload, t, issues)
    _check_id(payload.get("id"), t.id_prefix, "$.id", issues)
    _check_eq(payload.get("object"), t.response_object, "$.object", issues)
    _check_nonneg_int(payload.get("created"), "$.created", issues)
    _check_model(payload.get("model"), request_model, "$.model", issues)
    _check_usage(payload.get("usage"), t.usage_required, "$.usage", issues)

    fp = payload.get("system_fingerprint")
    if fp is not None and not (
        isinstance(fp, str) and fp.startswith("fp_")
    ):
        issues.append(
            ProtocolIssue(
                "minor",
                "system_fingerprint_invalid",
                "$.system_fingerprint",
                "system_fingerprint, when present, should use the fp_ prefix",
                expected="fp_* or null",
                actual=fp,
            )
        )

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        issues.append(
            ProtocolIssue(
                "critical",
                "choices_missing_or_empty",
                "$.choices",
                "Chat Completions response must include at least one choice",
                expected="non-empty array",
                actual=type(choices).__name__,
            )
        )
    else:
        for i, choice in enumerate(choices):
            _check_chat_choice(choice, i, t, issues)

    fingerprints = _chat_fingerprints(payload)
    return TemplateValidation(
        wire_api=t.wire_api,
        template_name=t.name,
        score=_score(issues),
        issues=issues,
        fingerprints=fingerprints,
    )


def _check_top_level(
    payload: dict[str, Any],
    template: ProtocolTemplate,
    issues: list[ProtocolIssue],
) -> None:
    if not isinstance(payload, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "payload_not_object",
                "$",
                "OpenAI response payload must be a JSON object",
                expected="object",
                actual=type(payload).__name__,
            )
        )
        return
    for key in template.required_top_level:
        if key not in payload:
            issues.append(
                ProtocolIssue(
                    "critical",
                    "top_level_missing",
                    f"$.{key}",
                    f"Missing required top-level field {key!r}",
                    expected="present",
                    actual=None,
                )
            )


def _check_id(
    value: Any,
    prefix: str,
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    if not (isinstance(value, str) and value.startswith(prefix)):
        issues.append(
            ProtocolIssue(
                "critical",
                "id_prefix_invalid",
                path,
                f"ID must use the official {prefix!r} prefix",
                expected=f"{prefix}*",
                actual=value,
            )
        )


def _check_eq(
    value: Any,
    expected: Any,
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    if value != expected:
        issues.append(
            ProtocolIssue(
                "critical",
                "object_invalid",
                path,
                "Response object discriminator does not match the template",
                expected=expected,
                actual=value,
            )
        )


def _check_nonneg_int(
    value: Any,
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        issues.append(
            ProtocolIssue(
                "major",
                "nonneg_int_invalid",
                path,
                "Expected a non-negative integer",
                expected="non-negative integer",
                actual=value,
            )
        )


def _check_model(
    value: Any,
    request_model: str | None,
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    if not isinstance(value, str) or not value:
        issues.append(
            ProtocolIssue(
                "critical",
                "model_missing_or_not_string",
                path,
                "model must be a non-empty string",
                expected="string",
                actual=value,
            )
        )
        return
    if request_model and not _models_match(request_model, value):
        issues.append(
            ProtocolIssue(
                "major",
                "model_mismatch",
                path,
                "response.model does not match the requested model",
                expected=request_model,
                actual=value,
            )
        )


def _check_usage(
    usage: Any,
    required: tuple[str, ...],
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    if not isinstance(usage, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "usage_missing_or_not_object",
                path,
                "usage must be an object with token counters",
                expected="object",
                actual=type(usage).__name__,
            )
        )
        return

    for key in required:
        _check_nonneg_int(usage.get(key), f"{path}.{key}", issues)

    total = usage.get("total_tokens")
    if required == ("input_tokens", "output_tokens", "total_tokens"):
        left = usage.get("input_tokens")
        right = usage.get("output_tokens")
    else:
        left = usage.get("prompt_tokens")
        right = usage.get("completion_tokens")

    if all(isinstance(v, int) and not isinstance(v, bool) for v in (left, right, total)):
        if total != left + right:
            issues.append(
                ProtocolIssue(
                    "minor",
                    "usage_total_mismatch",
                    f"{path}.total_tokens",
                    "total_tokens should equal input/prompt plus output/completion tokens",
                    expected=left + right,
                    actual=total,
                )
            )

    _check_usage_adapter_fingerprints(usage, required, path, issues)


def _check_usage_adapter_fingerprints(
    usage: dict[str, Any],
    required: tuple[str, ...],
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    """Flag provider-adapter fingerprints inside OpenAI usage objects.

    Official OpenAI Chat Completions usage carries `prompt_tokens`,
    `completion_tokens`, `total_tokens` and optional detail objects. Anything
    else — Claude-specific cache fields, Gemini-named counters, an explicit
    `usage_source` pointing at another vendor — means the relay is translating
    from a different upstream and forgot to scrub the residue. We treat these
    as **critical** because they reveal the relay is not running OpenAI at all,
    only impersonating it. A single critical flips ProtocolDetector to fail
    even when every required field is technically present.
    """
    keys = set(usage)

    claude_keys = sorted(k for k in keys if k.startswith("claude_"))
    if claude_keys:
        issues.append(
            ProtocolIssue(
                "critical",
                "usage_contains_claude_fields",
                path,
                "OpenAI usage contains Claude-specific fields — relay likely translates from an Anthropic backend",
                expected="no claude_* usage fields",
                actual=claude_keys,
            )
        )

    gemini_keys = sorted(
        k for k in keys
        if k.startswith("gemini_") or k in (
            "cached_content_token_count",
            "candidates_token_count",
            "prompt_token_count",
            "thoughts_token_count",
            "tool_use_prompt_token_count",
        )
    )
    if gemini_keys:
        issues.append(
            ProtocolIssue(
                "critical",
                "usage_contains_gemini_fields",
                path,
                "OpenAI usage contains Gemini-specific fields — relay likely translates from a Google backend",
                expected="no gemini_* / *_token_count usage fields",
                actual=gemini_keys,
            )
        )

    source = usage.get("usage_source")
    if isinstance(source, str):
        s = source.lower()
        # Empty string / "openai" are fine; anything else names a different
        # backend and is a smoking gun.
        if s and s != "openai":
            issues.append(
                ProtocolIssue(
                    "critical",
                    "usage_source_non_openai",
                    f"{path}.usage_source",
                    f"OpenAI usage declares upstream source is {source!r} — relay is impersonating OpenAI",
                    expected="OpenAI usage without explicit non-OpenAI source marker",
                    actual=source,
                )
            )

    # Chat Completions uses prompt_tokens / completion_tokens. The
    # input_tokens / output_tokens pair belongs to Responses/Anthropic-shaped
    # envelopes; when it appears in a chat.completion usage object alongside
    # prompt/completion tokens, it is a mixed-shape adapter fingerprint.
    if required == ("prompt_tokens", "completion_tokens", "total_tokens"):
        mixed = sorted(keys & {"input_tokens", "output_tokens", "input_tokens_details"})
        if mixed:
            issues.append(
                ProtocolIssue(
                    "minor",
                    "usage_mixed_token_fields",
                    path,
                    "Chat Completions usage mixes OpenAI fields with input/output token fields",
                    expected="prompt_tokens/completion_tokens/total_tokens",
                    actual=mixed,
                )
            )


def _check_response_output_item(
    item: Any,
    index: int,
    template: ProtocolTemplate,
    issues: list[ProtocolIssue],
) -> None:
    path = f"$.output[{index}]"
    if not isinstance(item, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "output_item_not_object",
                path,
                "Each Responses API output item must be an object",
                expected="object",
                actual=type(item).__name__,
            )
        )
        return

    item_type = item.get("type")
    if item_type not in template.allowed_output_item_types:
        issues.append(
            ProtocolIssue(
                "major",
                "output_item_type_unknown",
                f"{path}.type",
                "Unknown Responses API output item type",
                expected=template.allowed_output_item_types,
                actual=item_type,
            )
        )
        return

    item_id = item.get("id")
    if item_id is not None and not isinstance(item_id, str):
        issues.append(
            ProtocolIssue(
                "minor",
                "output_item_id_not_string",
                f"{path}.id",
                "output item id should be a string when present",
                expected="string",
                actual=item_id,
            )
        )

    if item_type == "message":
        if item.get("role") != "assistant":
            issues.append(
                ProtocolIssue(
                    "major",
                    "message_role_invalid",
                    f"{path}.role",
                    "Responses API output messages should use role=assistant",
                    expected="assistant",
                    actual=item.get("role"),
                )
            )
        content = item.get("content")
        if not isinstance(content, list):
            issues.append(
                ProtocolIssue(
                    "critical",
                    "message_content_not_array",
                    f"{path}.content",
                    "Responses API message content must be an array",
                    expected="array",
                    actual=type(content).__name__,
                )
            )
        else:
            for j, content_item in enumerate(content):
                _check_response_content_item(
                    content_item, f"{path}.content[{j}]", template, issues
                )
    elif item_type == "function_call":
        _check_function_call_item(item, path, issues)


def _check_response_content_item(
    item: Any,
    path: str,
    template: ProtocolTemplate,
    issues: list[ProtocolIssue],
) -> None:
    if not isinstance(item, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "content_item_not_object",
                path,
                "content item must be an object",
                expected="object",
                actual=type(item).__name__,
            )
        )
        return

    item_type = item.get("type")
    if item_type not in template.allowed_content_item_types:
        issues.append(
            ProtocolIssue(
                "major",
                "content_item_type_unknown",
                f"{path}.type",
                "Unknown Responses API content item type",
                expected=template.allowed_content_item_types,
                actual=item_type,
            )
        )
        return

    if item_type == "output_text" and not isinstance(item.get("text"), str):
        issues.append(
            ProtocolIssue(
                "major",
                "output_text_missing",
                f"{path}.text",
                "output_text content item must include text",
                expected="string",
                actual=item.get("text"),
            )
        )
    if item_type == "refusal" and not isinstance(item.get("refusal"), str):
        issues.append(
            ProtocolIssue(
                "major",
                "refusal_missing",
                f"{path}.refusal",
                "refusal content item must include refusal text",
                expected="string",
                actual=item.get("refusal"),
            )
        )


def _check_function_call_item(
    item: dict[str, Any],
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    for key in ("call_id", "name", "arguments"):
        if not isinstance(item.get(key), str) or not item.get(key):
            issues.append(
                ProtocolIssue(
                    "major",
                    "function_call_field_invalid",
                    f"{path}.{key}",
                    f"function_call output item must include string {key!r}",
                    expected="non-empty string",
                    actual=item.get(key),
                )
            )

    call_id = item.get("call_id")
    if isinstance(call_id, str) and not call_id.startswith("call_"):
        issues.append(
            ProtocolIssue(
                "minor",
                "function_call_id_prefix_invalid",
                f"{path}.call_id",
                "OpenAI function call ids normally use the call_ prefix",
                expected="call_*",
                actual=call_id,
            )
        )

    args = item.get("arguments")
    if isinstance(args, str):
        _check_json_object_string(args, f"{path}.arguments", issues)


def _check_chat_choice(
    choice: Any,
    index: int,
    template: ProtocolTemplate,
    issues: list[ProtocolIssue],
) -> None:
    path = f"$.choices[{index}]"
    if not isinstance(choice, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "choice_not_object",
                path,
                "Each choice must be an object",
                expected="object",
                actual=type(choice).__name__,
            )
        )
        return

    _check_nonneg_int(choice.get("index"), f"{path}.index", issues)
    finish_reason = choice.get("finish_reason")
    if finish_reason not in template.allowed_finish_reasons:
        issues.append(
            ProtocolIssue(
                "major",
                "finish_reason_invalid",
                f"{path}.finish_reason",
                "finish_reason must be an official Chat Completions enum value",
                expected=template.allowed_finish_reasons,
                actual=finish_reason,
            )
        )

    message = choice.get("message")
    if not isinstance(message, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "message_missing_or_not_object",
                f"{path}.message",
                "choice.message must be an object",
                expected="object",
                actual=type(message).__name__,
            )
        )
        return

    if message.get("role") != "assistant":
        issues.append(
            ProtocolIssue(
                "major",
                "message_role_invalid",
                f"{path}.message.role",
                "Chat completion message should use role=assistant",
                expected="assistant",
                actual=message.get("role"),
            )
        )

    content = message.get("content")
    tool_calls = message.get("tool_calls")
    refusal = message.get("refusal")
    if content is not None and not isinstance(content, (str, list)):
        issues.append(
            ProtocolIssue(
                "major",
                "message_content_invalid",
                f"{path}.message.content",
                "message.content must be string, array, or null",
                expected="string | array | null",
                actual=type(content).__name__,
            )
        )
    if refusal is not None and not isinstance(refusal, str):
        issues.append(
            ProtocolIssue(
                "minor",
                "message_refusal_invalid",
                f"{path}.message.refusal",
                "message.refusal must be a string when present",
                expected="string",
                actual=refusal,
            )
        )

    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            issues.append(
                ProtocolIssue(
                    "critical",
                    "tool_calls_not_array",
                    f"{path}.message.tool_calls",
                    "message.tool_calls must be an array when present",
                    expected="array",
                    actual=type(tool_calls).__name__,
                )
            )
        else:
            for j, tool_call in enumerate(tool_calls):
                _check_chat_tool_call(
                    tool_call, f"{path}.message.tool_calls[{j}]", issues
                )
            if finish_reason not in ("tool_calls", None):
                issues.append(
                    ProtocolIssue(
                        "minor",
                        "tool_calls_finish_reason_mismatch",
                        f"{path}.finish_reason",
                        "finish_reason should be tool_calls when tool_calls are returned",
                        expected="tool_calls",
                        actual=finish_reason,
                    )
                )


def _check_chat_tool_call(
    tool_call: Any,
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    if not isinstance(tool_call, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "tool_call_not_object",
                path,
                "tool call must be an object",
                expected="object",
                actual=type(tool_call).__name__,
            )
        )
        return

    _check_id(tool_call.get("id"), "call_", f"{path}.id", issues)
    if tool_call.get("type") != "function":
        issues.append(
            ProtocolIssue(
                "major",
                "tool_call_type_invalid",
                f"{path}.type",
                "Chat Completions tool calls should use type=function",
                expected="function",
                actual=tool_call.get("type"),
            )
        )

    fn = tool_call.get("function")
    if not isinstance(fn, dict):
        issues.append(
            ProtocolIssue(
                "critical",
                "tool_call_function_missing",
                f"{path}.function",
                "tool call must include a function object",
                expected="object",
                actual=type(fn).__name__,
            )
        )
        return

    if not isinstance(fn.get("name"), str) or not fn.get("name"):
        issues.append(
            ProtocolIssue(
                "major",
                "tool_call_function_name_invalid",
                f"{path}.function.name",
                "tool call function name must be a non-empty string",
                expected="non-empty string",
                actual=fn.get("name"),
            )
        )
    args = fn.get("arguments")
    if not isinstance(args, str):
        issues.append(
            ProtocolIssue(
                "major",
                "tool_call_arguments_not_string",
                f"{path}.function.arguments",
                "tool call function arguments must be a JSON string",
                expected="JSON object string",
                actual=type(args).__name__,
            )
        )
    else:
        _check_json_object_string(args, f"{path}.function.arguments", issues)


def _check_json_object_string(
    value: str,
    path: str,
    issues: list[ProtocolIssue],
) -> None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        issues.append(
            ProtocolIssue(
                "major",
                "json_arguments_invalid",
                path,
                "Tool/function arguments must parse as JSON",
                expected="valid JSON object string",
                actual=f"{type(e).__name__}: {e.msg}",
            )
        )
        return
    if not isinstance(parsed, dict):
        issues.append(
            ProtocolIssue(
                "major",
                "json_arguments_not_object",
                path,
                "Tool/function arguments should be a JSON object",
                expected="JSON object",
                actual=type(parsed).__name__,
            )
        )


def _models_match(request_model: str, response_model: str) -> bool:
    return response_model.startswith(request_model) or request_model.startswith(
        response_model
    )


def _score(issues: list[ProtocolIssue]) -> float:
    penalty = 0.0
    for issue in issues:
        if issue.severity == "critical":
            penalty += 35.0
        elif issue.severity == "major":
            penalty += 15.0
        else:
            penalty += 5.0
    return max(0.0, 100.0 - penalty)


def _responses_fingerprints(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    output_types = [
        item.get("type")
        for item in output
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    ]
    content_types: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("type"), str):
                content_types.append(content["type"])
    return {
        "id_prefix": _prefix(payload.get("id")),
        "object": payload.get("object"),
        "status": payload.get("status"),
        "model": payload.get("model"),
        "output_types": output_types,
        "content_types": content_types,
        "usage_keys": sorted(usage.keys()),
    }


def _chat_fingerprints(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    finish_reasons = [
        choice.get("finish_reason")
        for choice in choices
        if isinstance(choice, dict)
    ]
    tool_call_id_prefixes: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                tool_call_id_prefixes.append(_prefix(tool_call.get("id")))
    return {
        "id_prefix": _prefix(payload.get("id")),
        "object": payload.get("object"),
        "model": payload.get("model"),
        "finish_reasons": finish_reasons,
        "system_fingerprint_present": isinstance(
            payload.get("system_fingerprint"), str
        ),
        "tool_call_id_prefixes": tool_call_id_prefixes,
        "usage_keys": sorted(usage.keys()),
    }


def _prefix(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("chatcmpl-"):
        return "chatcmpl-"
    if value.startswith("resp_"):
        return "resp_"
    if value.startswith("msg_"):
        return "msg_"
    if value.startswith("call_"):
        return "call_"
    if "_" in value:
        return value.split("_", 1)[0] + "_"
    if "-" in value:
        return value.split("-", 1)[0] + "-"
    return value[:12]
