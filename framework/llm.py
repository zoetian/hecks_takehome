"""OpenRouter API client for LLM inference.

This module provides a client for the OpenRouter API, supporting
streaming chat completions with tool calling.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# Set up logging for rate limit retries (logs to stderr when retrying)
_logger = logging.getLogger(__name__)
if not _logger.handlers:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    _logger.setLevel(logging.WARNING)


def _is_rate_limit_error(exception: BaseException) -> bool:
    """Check if the exception is a rate limit (429) error."""
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code == 429
    return False


def _is_read_timeout_error(exception: BaseException) -> bool:
    """Check if the exception is a read timeout error."""
    return isinstance(exception, httpx.ReadTimeout)


# v0: fix provider error handling under concurrency.
# Retry transient upstream/provider errors: 408, 429, and 5xx. The response body is
# now included in the error (see _raise_for_status_with_body) so the cause shows up
# in eval traces.
# v1: 400 removed from the retry set. The v0 400s turned out to be deterministic
# (an empty tool_call_id caused by a tool-call streaming bug, fixed in v1), so
# retrying only added ~15s of backoff before the same failure. A 400 now fails fast.
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _is_retryable_status_error(exception: BaseException) -> bool:
    """Check if the exception is an HTTP error with a retryable status code."""
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code in _RETRYABLE_STATUS_CODES
    return False


def _is_in_flight_budget_error(exception: BaseException) -> bool:
    """402 caused by OpenRouter's in-flight credit reservation, not by an empty balance.

    v3a-infra: OpenRouter reserves credit for every in-flight request at its maximum
    possible cost. Under concurrency this returns 402 `in_flight_budget_exhausted` with a
    Retry-After header. It clears once other requests settle, so it is transient. A
    plain 402 (actually out of credits) still fails fast.
    """
    return (
        isinstance(exception, httpx.HTTPStatusError)
        and exception.response.status_code == 402
        and "in_flight_budget_exhausted" in str(exception)
    )


def _is_retryable_error(exception: BaseException) -> bool:
    """Check if the exception is retryable (retryable HTTP status or read timeout)."""
    return (
        _is_rate_limit_error(exception)
        or _is_retryable_status_error(exception)
        or _is_in_flight_budget_error(exception)
        or _is_read_timeout_error(exception)
    )


_exponential_wait = wait_exponential(multiplier=1, min=1, max=60)


def _retry_wait(retry_state: RetryCallState) -> float:
    """Exponential backoff, but honor Retry-After (capped) for in-flight budget 402s."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if exc is not None and _is_in_flight_budget_error(exc):
        assert isinstance(exc, httpx.HTTPStatusError)
        try:
            retry_after = float(exc.response.headers.get("Retry-After", "30"))
        except ValueError:
            retry_after = 30.0
        return min(max(retry_after, 5.0), 120.0)
    return _exponential_wait(retry_state)


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry so transient provider errors are visible on stderr."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    _logger.warning(
        "OpenRouter request failed (attempt %d), retrying: %s",
        retry_state.attempt_number,
        str(exc).splitlines()[0] if exc else "unknown error",
    )


def _raise_for_status_with_body(response: httpx.Response) -> None:
    """Like response.raise_for_status(), but include the response body in the message.

    v0: the default error message drops the body, which hid why OpenRouter
    returned 400 in the baseline run.
    """
    if response.is_success:
        return
    try:
        body = response.read().decode("utf-8", errors="replace")[:1000]
    except Exception:
        body = "<unreadable body>"
    raise httpx.HTTPStatusError(
        f"HTTP {response.status_code} from OpenRouter: {body}",
        request=response.request,
        response=response,
    )


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class OpenRouterConfig:
    """Configuration for the OpenRouter API client.

    Defaults to gpt-oss-120b on Cerebras provider.
    """

    model: str = "openai/gpt-oss-120b:nitro"
    api_key: str = ""
    # v3a-infra: was 100000. OpenRouter reserves credit per in-flight request at
    # max_tokens, so 16 workers x 100k tokens exhausted the in-flight budget (HTTP 402
    # in_flight_budget_exhausted on ~95% of requests). Observed completions per
    # generation in v2: median 169, p99 945, max 1,563. 16k leaves 10x headroom.
    max_tokens: int = 16384
    # v4: was 0.6. Across v2/v3a/v3b, ~20 of 64 hard cases flipped between pass and fail
    # while only 3 failed consistently, so sampling variance is the largest remaining
    # source of error. Lower temperature makes the agent's choices (which guide rule to
    # apply, which table to use) more repeatable. We avoid 0.0, which can cause
    # repetition loops in reasoning models.
    temperature: float = 0.2
    max_iterations: int = 30
    # Provider routing - no restriction by default
    provider: dict[str, Any] = field(default_factory=dict)
    # Reasoning configuration
    reasoning: dict[str, Any] | None = None
    # Timeout for receiving first token (also applies between subsequent chunks)
    # If no data is received within this time, the request will timeout
    first_token_timeout: float = 10.0
    # Context compression settings (reduces token usage on long conversations)
    compress_context: bool = False  # Enable context compression
    compress_keep_recent: int = 3  # Number of recent tool results to keep in full
    compress_max_chars: int = 150  # Max chars for truncated older results


@dataclass
class TokenUsage:
    """Token usage statistics from an API call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens used (prompt + completion)."""
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """Add two TokenUsage instances together."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass
class StreamChunk:
    """A single chunk from a streaming response."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_details: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class OpenRouterClient:
    """Client for the OpenRouter API with streaming support."""

    def __init__(self, config: OpenRouterConfig) -> None:
        """Initialize the client with configuration."""
        self.config = config
        if not config.api_key:
            raise ValueError("OpenRouter API key is required")

        self._client = httpx.Client(
            timeout=httpx.Timeout(
                300.0,  # Overall timeout
                connect=30.0,  # Connection establishment timeout
                read=config.first_token_timeout,  # Time to wait for data between chunks
            ),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/hex-inc/takehome",
                "X-Title": "Hex Takehome Agent",
            },
        )

    def _build_request_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Build the request body for the API call."""
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": stream,
        }

        # Request usage data in streaming mode (OpenAI-compatible extension)
        if stream:
            body["stream_options"] = {"include_usage": True}

        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        if self.config.provider:
            body["provider"] = self.config.provider

        if self.config.reasoning:
            body["reasoning"] = self.config.reasoning

        return body

    def chat_completion_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamChunk]:
        """Make a streaming chat completion request.

        Yields StreamChunk objects as they arrive via SSE.
        """
        body = self._build_request_body(messages, tools, stream=True)

        # Use Retrying for retryable errors (rate limits, read timeouts) with streaming
        retryer = Retrying(
            retry=retry_if_exception(_is_retryable_error),
            stop=stop_after_attempt(5),
            wait=_retry_wait,  # v3a-infra: honors Retry-After for in-flight budget 402s
            before_sleep=_log_retry,  # v0: make provider retries visible
            reraise=True,
        )

        try:
            for attempt in retryer:
                with attempt:
                    with self._client.stream(
                        "POST", OPENROUTER_API_URL, json=body
                    ) as response:
                        # v0: keep the error body (was response.raise_for_status())
                        _raise_for_status_with_body(response)

                        # Accumulated tool call data (streamed incrementally)
                        tool_calls_buffer: dict[int, dict[str, Any]] = {}
                        # v1: maps a provider's spurious continuation index to the real one
                        index_alias: dict[int, int] = {}

                        for line in response.iter_lines():
                            # Skip empty lines and comments
                            if not line or line.startswith(":"):
                                continue

                            # SSE format: "data: {json}"
                            if not line.startswith("data: "):
                                continue

                            data_str = line[6:]  # Remove "data: " prefix

                            # Handle stream end
                            if data_str == "[DONE]":
                                break

                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            # Extract from choices[0].delta
                            choices = data.get("choices", [])
                            if not choices:
                                continue

                            choice = choices[0]
                            delta = choice.get("delta", {})
                            finish_reason = choice.get("finish_reason")

                            chunk = StreamChunk(finish_reason=finish_reason)

                            # Content
                            if "content" in delta and delta["content"]:
                                chunk.content = delta["content"]

                            # Reasoning details
                            if "reasoning_details" in delta and delta["reasoning_details"]:
                                chunk.reasoning_details = delta["reasoning_details"]

                            # Tool calls (accumulated incrementally)
                            if "tool_calls" in delta and delta["tool_calls"]:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    # v1: some providers (seen: Nebius) emit the tail of a
                                    # call's arguments under a NEW index with an empty id
                                    # and name, e.g. a lone "}". Treating it as a separate
                                    # call breaks both calls: the first gets invalid JSON,
                                    # and the phantom one has an empty tool_call_id, which
                                    # makes the next request fail with 400. A new index
                                    # without an id or name is a continuation, so route it
                                    # to the previous call.
                                    idx = index_alias.get(idx, idx)
                                    func_delta = tc.get("function") or {}
                                    if (
                                        idx not in tool_calls_buffer
                                        and tool_calls_buffer
                                        and not tc.get("id")
                                        and not func_delta.get("name")
                                    ):
                                        last_idx = max(tool_calls_buffer)
                                        index_alias[idx] = last_idx
                                        idx = last_idx
                                    if idx not in tool_calls_buffer:
                                        tool_calls_buffer[idx] = {
                                            "id": tc.get("id", ""),
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }

                                    if "id" in tc and tc["id"]:
                                        tool_calls_buffer[idx]["id"] = tc["id"]

                                    if "function" in tc:
                                        func = tc["function"]
                                        if "name" in func and func["name"]:
                                            tool_calls_buffer[idx]["function"][
                                                "name"
                                            ] = func["name"]
                                        if "arguments" in func:
                                            tool_calls_buffer[idx]["function"][
                                                "arguments"
                                            ] += func["arguments"]

                            # When finish_reason is set, include accumulated tool calls
                            if finish_reason == "tool_calls" and tool_calls_buffer:
                                chunk.tool_calls = [
                                    tool_calls_buffer[i]
                                    for i in sorted(tool_calls_buffer.keys())
                                ]
                                # v1: the API rejects tool messages with an empty
                                # tool_call_id, so fill in a synthetic id if the
                                # provider never sent one.
                                for call in chunk.tool_calls:
                                    if not call["id"]:
                                        call["id"] = f"call_{uuid.uuid4().hex[:24]}"

                            # Parse usage data if present (comes in final chunk)
                            if "usage" in data and data["usage"]:
                                usage_data = data["usage"]
                                chunk.usage = TokenUsage(
                                    prompt_tokens=usage_data.get("prompt_tokens", 0),
                                    completion_tokens=usage_data.get(
                                        "completion_tokens", 0
                                    ),
                                )

                            yield chunk
        except KeyboardInterrupt:
            # Re-raise to allow proper cleanup at higher levels
            raise

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()
