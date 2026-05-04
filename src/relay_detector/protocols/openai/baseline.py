"""Official OpenAI baseline probes and feature extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol

import httpx

from .client import OpenAIAPIError
from .protocol_templates import WireAPI, validate_openai_payload


BaselineWireAPI = Literal["responses", "chat_completions", "both"]
ProbeSet = Literal["smoke", "full"]

SELECTED_RESPONSE_HEADERS = {
    "date",
    "server",
    "x-request-id",
    "request-id",
    "openai-request-id",
    "openai-processing-ms",
    "openai-version",
    "openai-model",
    "cf-ray",
    "retry-after",
}


class OpenAIBaselineClient(Protocol):
    async def responses_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        ...

    async def chat_completions_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        ...


@dataclass(frozen=True)
class OpenAIProbe:
    name: str
    wire_api: WireAPI
    request: dict[str, Any]


def build_openai_baseline_probes(
    model: str,
    *,
    wire_api: BaselineWireAPI = "both",
    probe_set: ProbeSet = "full",
) -> list[OpenAIProbe]:
    """Build low-cost probes that exercise official OpenAI wire features."""

    if wire_api not in ("responses", "chat_completions", "both"):
        raise ValueError("wire_api must be responses, chat_completions, or both")
    if probe_set not in ("smoke", "full"):
        raise ValueError("probe_set must be smoke or full")

    probes: list[OpenAIProbe] = []
    include_responses = wire_api in ("responses", "both")
    include_chat = wire_api in ("chat_completions", "both")

    if include_responses:
        probes.append(
            OpenAIProbe(
                name="responses_text",
                wire_api="responses",
                request={
                    "model": model,
                    "input": "Reply with exactly: pong",
                    "max_output_tokens": 32,
                    "store": False,
                },
            )
        )
        if probe_set == "full":
            probes.append(
                OpenAIProbe(
                    name="responses_structured_output",
                    wire_api="responses",
                    request={
                        "model": model,
                        "input": (
                            "Return JSON matching the schema with ok=true "
                            'and nonce="openai-baseline".'
                        ),
                        "max_output_tokens": 128,
                        "store": False,
                        "text": {"format": _responses_json_schema_format()},
                    },
                )
            )
            probes.append(
                OpenAIProbe(
                    name="responses_tool_call",
                    wire_api="responses",
                    request={
                        "model": model,
                        "input": (
                            "Use get_current_weather for Boston, MA in celsius. "
                            "Do not answer directly."
                        ),
                        "max_output_tokens": 128,
                        "store": False,
                        "tools": [_responses_weather_tool()],
                        "tool_choice": {
                            "type": "function",
                            "name": "get_current_weather",
                        },
                    },
                )
            )

    if include_chat:
        probes.append(
            OpenAIProbe(
                name="chat_text",
                wire_api="chat_completions",
                request={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                    "max_completion_tokens": 32,
                    "store": False,
                },
            )
        )
        if probe_set == "full":
            probes.append(
                OpenAIProbe(
                    name="chat_structured_output",
                    wire_api="chat_completions",
                    request={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "Return JSON matching the schema with ok=true "
                                    'and nonce="openai-baseline".'
                                ),
                            }
                        ],
                        "max_completion_tokens": 128,
                        "store": False,
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": _chat_json_schema_format(),
                        },
                    },
                )
            )
            probes.append(
                OpenAIProbe(
                    name="chat_tool_call",
                    wire_api="chat_completions",
                    request={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "Use get_current_weather for Boston, MA in celsius. "
                                    "Do not answer directly."
                                ),
                            }
                        ],
                        "max_completion_tokens": 128,
                        "store": False,
                        "tools": [_chat_weather_tool()],
                        "tool_choice": {
                            "type": "function",
                            "function": {"name": "get_current_weather"},
                        },
                    },
                )
            )

    return probes


async def collect_openai_official_baseline(
    client: OpenAIBaselineClient,
    *,
    base_url: str,
    api_key_masked: str,
    model: str,
    wire_api: BaselineWireAPI = "both",
    probe_set: ProbeSet = "full",
) -> dict[str, Any]:
    """Run official OpenAI probes and return a JSON-serializable report."""

    probes = build_openai_baseline_probes(
        model,
        wire_api=wire_api,
        probe_set=probe_set,
    )
    results = []
    for probe in probes:
        results.append(await _run_probe(client, probe, request_model=model))

    report = {
        "provider": "openai",
        "base_url": base_url.rstrip("/"),
        "api_key_masked": api_key_masked,
        "target_model": model,
        "wire_api": wire_api,
        "probe_set": probe_set,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "probes": results,
    }
    report["summary"] = summarize_openai_baseline(results)
    return report


async def _run_probe(
    client: OpenAIBaselineClient,
    probe: OpenAIProbe,
    *,
    request_model: str,
) -> dict[str, Any]:
    try:
        if probe.wire_api == "responses":
            request, response, headers, latency_ms = await client.responses_create(
                **probe.request
            )
        else:
            request, response, headers, latency_ms = await client.chat_completions_create(
                **probe.request
            )
    except OpenAIAPIError as e:
        return {
            "name": probe.name,
            "wire_api": probe.wire_api,
            "ok": False,
            "request": probe.request,
            "headers": sanitize_openai_headers(e.headers or {}),
            "error": {
                "type": "OpenAIAPIError",
                "status": e.status,
                "body": e.body[:2000],
            },
        }
    except Exception as e:
        return {
            "name": probe.name,
            "wire_api": probe.wire_api,
            "ok": False,
            "request": probe.request,
            "headers": {},
            "error": {
                "type": type(e).__name__,
                "message": str(e)[:1000],
            },
        }

    validation = validate_openai_payload(
        probe.wire_api,
        response,
        request_model=request_model,
    )
    safe_headers = sanitize_openai_headers(headers)
    features = extract_openai_features(probe.wire_api, response, safe_headers)

    return {
        "name": probe.name,
        "wire_api": probe.wire_api,
        "ok": True,
        "request": request,
        "response": response,
        "headers": safe_headers,
        "latency_ms": latency_ms,
        "validation": validation.to_dict(),
        "features": features,
    }


def sanitize_openai_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep only diagnostic headers; never persist auth/cookie headers."""

    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in SELECTED_RESPONSE_HEADERS or lower.startswith("x-ratelimit-"):
            sanitized[lower] = value
    return dict(sorted(sanitized.items()))


def extract_openai_features(
    wire_api: WireAPI,
    payload: dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Extract stable protocol fingerprints from one raw response."""

    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}

    features: dict[str, Any] = {
        "top_level_keys": sorted(payload.keys()),
        "id_prefix": _prefix(payload.get("id")),
        "object": payload.get("object"),
        "model": payload.get("model"),
        "usage_keys": _dict_keys(payload.get("usage")),
        "response_header_keys": sorted((headers or {}).keys()),
    }
    if wire_api == "responses":
        _add_responses_features(features, payload)
    else:
        _add_chat_features(features, payload)
    return features


def summarize_openai_baseline(results: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [
        p.get("validation", {}).get("score")
        for p in results
        if p.get("ok") and isinstance(p.get("validation"), dict)
    ]
    numeric_scores = [float(s) for s in scores if isinstance(s, (int, float))]
    return {
        "probe_count": len(results),
        "ok_count": sum(1 for p in results if p.get("ok")),
        "passed_count": sum(
            1 for p in results if p.get("validation", {}).get("passed") is True
        ),
        "failed_probe_names": [p.get("name") for p in results if not p.get("ok")],
        "average_validation_score": (
            round(sum(numeric_scores) / len(numeric_scores), 2)
            if numeric_scores
            else None
        ),
        "seen_wire_apis": sorted({p.get("wire_api") for p in results if p.get("ok")}),
    }


def _add_responses_features(features: dict[str, Any], payload: dict[str, Any]) -> None:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    output_items = [item for item in output if isinstance(item, dict)]
    output_types = [item.get("type") for item in output_items]
    content_types: list[str] = []
    output_item_statuses: list[Any] = []
    function_call_names: list[str] = []
    function_call_id_prefixes: list[str] = []
    texts: list[str] = []

    for item in output_items:
        if item.get("status") is not None:
            output_item_statuses.append(item.get("status"))
        if item.get("type") == "function_call":
            if isinstance(item.get("name"), str):
                function_call_names.append(item["name"])
            function_call_id_prefixes.append(_prefix(item.get("call_id")))
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            content_types.append(content.get("type"))
            if isinstance(content.get("text"), str):
                texts.append(content["text"])

    features.update(
        {
            "status": payload.get("status"),
            "completed_at_present": payload.get("completed_at") is not None,
            "parallel_tool_calls": payload.get("parallel_tool_calls"),
            "tool_choice": payload.get("tool_choice"),
            "tool_types": [
                tool.get("type")
                for tool in payload.get("tools") or []
                if isinstance(tool, dict)
            ],
            "output_item_types": output_types,
            "output_item_statuses": output_item_statuses,
            "content_item_types": content_types,
            "function_call_seen": "function_call" in output_types,
            "function_call_names": function_call_names,
            "function_call_id_prefixes": function_call_id_prefixes,
            "usage_detail_keys": {
                "input_tokens_details": _dict_keys(usage.get("input_tokens_details")),
                "output_tokens_details": _dict_keys(usage.get("output_tokens_details")),
            },
            "first_output_text_is_json_object": _is_json_object(texts[0]) if texts else False,
        }
    )


def _add_chat_features(features: dict[str, Any], payload: dict[str, Any]) -> None:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    finish_reasons: list[Any] = []
    message_keys: list[list[str]] = []
    tool_call_names: list[str] = []
    tool_call_id_prefixes: list[str] = []
    text_values: list[str] = []

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        finish_reasons.append(choice.get("finish_reason"))
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        message_keys.append(sorted(message.keys()))
        content = message.get("content")
        if isinstance(content, str):
            text_values.append(content)
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id_prefixes.append(_prefix(tool_call.get("id")))
            function = tool_call.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                tool_call_names.append(function["name"])

    features.update(
        {
            "finish_reasons": finish_reasons,
            "system_fingerprint_prefix": _prefix(payload.get("system_fingerprint")),
            "choice_count": len(choices),
            "message_keys": message_keys,
            "tool_call_seen": bool(tool_call_names),
            "tool_call_names": tool_call_names,
            "tool_call_id_prefixes": tool_call_id_prefixes,
            "usage_detail_keys": {
                "prompt_tokens_details": _dict_keys(usage.get("prompt_tokens_details")),
                "completion_tokens_details": _dict_keys(
                    usage.get("completion_tokens_details")
                ),
            },
            "first_message_text_is_json_object": (
                _is_json_object(text_values[0]) if text_values else False
            ),
        }
    )


def _responses_json_schema_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "relay_detector_probe",
        "strict": True,
        "schema": _probe_schema(),
    }


def _chat_json_schema_format() -> dict[str, Any]:
    return {
        "name": "relay_detector_probe",
        "strict": True,
        "schema": _probe_schema(),
    }


def _probe_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "nonce": {"type": "string"},
        },
        "required": ["ok", "nonce"],
        "additionalProperties": False,
    }


def _weather_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City and region, e.g. Boston, MA",
            },
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["location", "unit"],
        "additionalProperties": False,
    }


def _responses_weather_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "get_current_weather",
        "description": "Get the current weather in a given location.",
        "parameters": _weather_parameters(),
        "strict": True,
    }


def _chat_weather_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather in a given location.",
            "parameters": _weather_parameters(),
            "strict": True,
        },
    }


def _dict_keys(value: Any) -> list[str]:
    return sorted(value.keys()) if isinstance(value, dict) else []


def _is_json_object(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _prefix(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    for prefix in ("chatcmpl-", "resp_", "msg_", "call_", "fc_", "fp_"):
        if value.startswith(prefix):
            return prefix
    if "_" in value:
        return value.split("_", 1)[0] + "_"
    if "-" in value:
        return value.split("-", 1)[0] + "-"
    return value[:12]
