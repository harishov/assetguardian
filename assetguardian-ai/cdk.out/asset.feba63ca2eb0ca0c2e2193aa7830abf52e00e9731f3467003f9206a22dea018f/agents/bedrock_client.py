"""Shared Bedrock Converse (Claude Sonnet 4, multi-modal) helper.

Every "agent" in this rebuild is a focused prompt + JSON-schema contract
sent through the same Converse API call, mirroring the report's
description of vision analysis being done via Claude Vision rather than
bespoke per-task models.
"""
import base64
import json
import logging
import os

import boto3

logger = logging.getLogger()

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")

_bedrock = boto3.client("bedrock-runtime")
_s3 = boto3.client("s3")


def _load_image_bytes(bucket: str, key: str) -> bytes:
    obj = _s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def _image_block(bucket: str, key: str) -> dict:
    fmt = "jpeg"
    lower = key.lower()
    if lower.endswith(".png"):
        fmt = "png"
    elif lower.endswith(".webp"):
        fmt = "webp"
    return {
        "image": {
            "format": fmt,
            "source": {"bytes": _load_image_bytes(bucket, key)},
        }
    }


def analyze_images(
    *,
    bucket: str,
    image_keys: list[str],
    system_prompt: str,
    instruction: str,
    response_schema_hint: str,
    max_tokens: int = 2000,
) -> dict:
    """
    Sends one or more images plus an instruction to Claude Sonnet 4 via the
    Converse API and parses a strict-JSON response.

    response_schema_hint is inlined into the prompt (not a hard JSON-schema
    constraint API) — Converse does not support forced structured output for
    all model families, so we ask for JSON and defensively parse it.
    """
    content = [{"text": instruction + "\n\nRespond with ONLY minified JSON matching this shape:\n" + response_schema_hint}]
    for key in image_keys:
        content.append(_image_block(bucket, key))

    response = _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.1},
    )

    text = "".join(
        block.get("text", "")
        for block in response["output"]["message"]["content"]
        if "text" in block
    )
    return _safe_json(text)


def _safe_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        logger.warning("Model response was not JSON, wrapping as raw text: %s", text[:500])
        return {"raw_response": text}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("Failed to parse model JSON response: %s", text[:500])
        return {"raw_response": text}
