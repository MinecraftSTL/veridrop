#!/usr/bin/env python3
"""Probe B.AI LLM API models and smoke-test OpenAI/Anthropic-compatible calls."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://api.b.ai"
DEFAULT_PROMPT = "Reply with exactly: pong"


def main() -> int:
    args = parse_args()
    api_key = args.api_key or os.getenv("BAI_API_KEY")
    if not api_key:
        print("Missing API key. Set BAI_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    headers = build_headers(api_key, args.auth)

    with httpx.Client(timeout=args.timeout) as client:
        models = list_models(client, base_url, headers)
        print_models(models)

        if args.list_only:
            return 0

        targets = select_targets(models, args)
        if not targets:
            print("No models matched the requested endpoint/model filters.", file=sys.stderr)
            return 1

        for model in targets:
            model_id = str(model.get("id", ""))
            endpoints = supported_endpoints(model)
            endpoint = choose_endpoint(args.endpoint, endpoints, model_id)
            if endpoint is None:
                print(f"\nSKIP {model_id}: no supported endpoint for --endpoint={args.endpoint}")
                continue

            print(f"\n== {model_id} via {endpoint} ==")
            try:
                result = call_model(
                    client=client,
                    base_url=base_url,
                    headers=headers,
                    endpoint=endpoint,
                    model=model_id,
                    prompt=args.prompt,
                    max_tokens=args.max_tokens,
                )
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:1000]
                print(f"HTTP {exc.response.status_code}: {body}")
                continue

            print(f"text: {result['text']}")
            if result.get("usage") is not None:
                print(f"usage: {result['usage']}")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List B.AI models and optionally smoke-test model calls."
    )
    parser.add_argument(
        "--api-key",
        help="B.AI API key. Defaults to BAI_API_KEY environment variable.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("BAI_API_BASE", DEFAULT_BASE_URL),
        help=f"API base URL. Defaults to {DEFAULT_BASE_URL}.",
    )
    parser.add_argument(
        "--auth",
        choices=("bearer", "x-api-key", "both"),
        default="bearer",
        help="Authentication header style. Docs support bearer and x-api-key.",
    )
    parser.add_argument(
        "--endpoint",
        choices=("auto", "openai", "anthropic"),
        default="auto",
        help="Which compatible endpoint to test.",
    )
    parser.add_argument(
        "--model",
        action="append",
        help="Model id to test. Can be passed multiple times. Defaults to one model per endpoint.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Smoke-test every listed model that matches --endpoint.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only call GET /v1/models; do not send chat/messages requests.",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def build_headers(api_key: str, auth: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if auth in ("bearer", "both"):
        headers["Authorization"] = f"Bearer {api_key}"
    if auth in ("x-api-key", "both"):
        headers["x-api-key"] = api_key
    return headers


def list_models(
    client: httpx.Client, base_url: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    response = client.get(f"{base_url}/v1/models", headers=headers)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"Unexpected /v1/models response: {payload}")
    return [item for item in data if isinstance(item, dict)]


def print_models(models: list[dict[str, Any]]) -> None:
    print(f"models: {len(models)}")
    for model in models:
        model_id = model.get("id", "")
        owner = model.get("owned_by", "")
        endpoints = ",".join(supported_endpoints(model)) or "-"
        print(f"- {model_id} owner={owner} endpoints={endpoints}")


def supported_endpoints(model: dict[str, Any]) -> list[str]:
    raw = model.get("supported_endpoint_types")
    if isinstance(raw, list):
        return [str(item) for item in raw if item in ("openai", "anthropic")]

    model_id = str(model.get("id", "")).lower()
    if model_id.startswith("claude-"):
        return ["anthropic"]
    return ["openai"]


def select_targets(
    models: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    if args.model:
        requested = set(args.model)
        return [model for model in models if model.get("id") in requested]

    if args.all:
        return [
            model
            for model in models
            if choose_endpoint(args.endpoint, supported_endpoints(model), str(model.get("id", "")))
        ]

    selected: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    for model in models:
        model_id = str(model.get("id", ""))
        endpoint = choose_endpoint(args.endpoint, supported_endpoints(model), model_id)
        if endpoint and endpoint not in seen_endpoints:
            selected.append(model)
            seen_endpoints.add(endpoint)
        if args.endpoint != "auto" and selected:
            break
        if seen_endpoints == {"openai", "anthropic"}:
            break
    return selected


def choose_endpoint(
    requested: str, endpoints: list[str], model_id: str
) -> str | None:
    if requested != "auto":
        return requested if requested in endpoints else None

    if "claude" in model_id.lower() and "anthropic" in endpoints:
        return "anthropic"
    if "openai" in endpoints:
        return "openai"
    if "anthropic" in endpoints:
        return "anthropic"
    return None


def call_model(
    *,
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    endpoint: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    if endpoint == "openai":
        return call_openai_chat(client, base_url, headers, model, prompt, max_tokens)
    if endpoint == "anthropic":
        return call_anthropic_messages(client, base_url, headers, model, prompt, max_tokens)
    raise ValueError(f"Unsupported endpoint: {endpoint}")


def call_openai_chat(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    response = client.post(
        f"{base_url}/v1/chat/completions", headers=headers, json=payload
    )
    response.raise_for_status()
    data = response.json()
    choice = data.get("choices", [{}])[0]
    message = choice.get("message") or {}
    return {
        "text": message.get("content", ""),
        "usage": data.get("usage"),
        "raw_model": data.get("model"),
    }


def call_anthropic_messages(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
        "temperature": 0,
    }
    response = client.post(f"{base_url}/v1/messages", headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    return {
        "text": extract_anthropic_text(data),
        "usage": data.get("usage"),
        "raw_model": data.get("model"),
    }


def extract_anthropic_text(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
