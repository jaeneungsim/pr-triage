"""One Bedrock Converse call that must answer through the submit_review tool."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import aws

TOOL_NAME = "submit_review"


class ModelOutputError(Exception):
    """The model answered, but not with the review tool."""

    def __init__(self, message: str, input_tokens: int, output_tokens: int):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class ReviewCall:
    review: dict[str, Any]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str


def request(model_id: str, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    return {
        "modelId": model_id,
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": user}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": TOOL_NAME,
                        "description": "Submit the review suggestion and its evidence. Always call this.",
                        "inputSchema": {"json": schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": TOOL_NAME}},
        },
    }


def call(model_id: str, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> ReviewCall:
    started = time.monotonic()
    response = aws.client("bedrock-runtime").converse(**request(model_id, system, user, schema, max_tokens))
    elapsed = int((time.monotonic() - started) * 1000)
    return parse(response, elapsed)


def parse(response: dict[str, Any], elapsed_ms: int = 0) -> ReviewCall:
    usage = response.get("usage", {})
    input_tokens, output_tokens = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
    for block in response.get("output", {}).get("message", {}).get("content", []):
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == TOOL_NAME and isinstance(tool_use.get("input"), dict):
            return ReviewCall(
                review=tool_use["input"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=int(response.get("metrics", {}).get("latencyMs", elapsed_ms)),
                stop_reason=str(response.get("stopReason", "")),
            )
    raise ModelOutputError(
        f"no {TOOL_NAME} call in the response (stop reason {response.get('stopReason')})", input_tokens, output_tokens
    )
