"""Experiment 8: Persistent Memory Across Sessions (4-strategy learning).

Thin wrapper around the Bedrock AgentCore Memory data-plane APIs
(CreateEvent / RetrieveMemoryRecords). Namespace isolation follows the
report's "{actorId}_reflections" convention. Fails soft: if
AGENTCORE_MEMORY_ID isn't configured (e.g. scripts/deploy_agentcore.py
hasn't run yet) memory calls are silently skipped rather than breaking the
inspection pipeline.
"""
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")

_client = boto3.client("bedrock-agentcore") if MEMORY_ID else None


def record_inspection_event(*, actor_id: str, session_id: str, payload: dict) -> None:
    """Writes an inspection outcome into AgentCore Memory so later sessions
    for the same employee/device can retrieve prior context (SEMANTIC /
    EPISODIC strategies) and so long-running sessions get summarized
    (SUMMARIZATION strategy)."""
    if not _client:
        logger.info("AGENTCORE_MEMORY_ID not set; skipping memory write.")
        return
    try:
        _client.create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[{"conversational": {"content": {"text": _to_text(payload)}, "role": "ASSISTANT"}}],
        )
    except Exception:
        logger.exception("Failed to write AgentCore memory event")


def retrieve_similar_inspections(*, actor_id: str, query_text: str, max_results: int = 5) -> list[dict]:
    """SEMANTIC memory retrieval: pulls related past inspections for this
    employee/device (e.g. "similar damage patterns to this laptop")."""
    if not _client:
        return []
    try:
        resp = _client.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=f"{actor_id}_reflections",
            searchCriteria={"searchQuery": query_text, "topK": max_results},
        )
        return resp.get("memoryRecordSummaries", [])
    except Exception:
        logger.exception("Failed to retrieve AgentCore memory records")
        return []


def _to_text(payload: dict) -> str:
    import json

    return json.dumps(payload, default=str)[:8000]
