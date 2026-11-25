"""Tests for streamed tool-call accumulation in OpenRouterClient."""

import json

import httpx

from framework.llm import OpenRouterClient, OpenRouterConfig


def _sse(*deltas: dict, finish: str = "tool_calls") -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [d]}, "finish_reason": None}]})
        for d in deltas
    ]
    lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _stream_tool_calls(body: bytes) -> list[dict]:
    client = OpenRouterClient(OpenRouterConfig(api_key="test"))
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    )
    tool_calls: list[dict] = []
    for chunk in client.chat_completion_stream([{"role": "user", "content": "hi"}]):
        if chunk.tool_calls:
            tool_calls = chunk.tool_calls
    return tool_calls


def test_spurious_continuation_index_is_merged() -> None:
    """Nebius-style stream: the closing '}' arrives under a new index with empty id/name."""
    body = _sse(
        {"index": 0, "id": "call_a", "type": "function",
         "function": {"name": "run_sql", "arguments": ""}},
        {"index": 0, "function": {"arguments": '{"query": "SELECT 1"\n'}},
        {"index": 1, "id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        {"index": 1, "function": {"arguments": "}"}},
    )
    calls = _stream_tool_calls(body)
    assert len(calls) == 1
    assert calls[0]["id"] == "call_a"
    assert calls[0]["function"]["name"] == "run_sql"
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "SELECT 1"}


def test_real_parallel_calls_are_kept_separate() -> None:
    body = _sse(
        {"index": 0, "id": "call_a", "function": {"name": "list_schemas", "arguments": "{}"}},
        {"index": 1, "id": "call_b", "function": {"name": "run_sql", "arguments": "{}"}},
    )
    calls = _stream_tool_calls(body)
    assert [c["id"] for c in calls] == ["call_a", "call_b"]


def test_missing_id_is_synthesized() -> None:
    body = _sse({"index": 0, "function": {"name": "list_schemas", "arguments": "{}"}})
    calls = _stream_tool_calls(body)
    assert len(calls) == 1 and calls[0]["id"]
