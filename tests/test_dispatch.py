"""AB-21-coo: unified dispatch through alfred-chat-stack gateway.

Verifies:
  * All three legacy model families (claude-*, qwen3-coder:cloud,
    openrouter/mistralai/...) now resolve to the same `_call_gateway`
    chokepoint with the correct model string forwarded.
  * Headers stamped on every request: X-Tiresias-Tenant, X-Alfred-Persona,
    Content-Type.
  * Authorization stamped when AUTOBUILD_SOULKEY is set; omitted (with a
    single warning) when it is empty — the gateway's allow-all policy
    covers the empty case while AB-21-gw rolls out.
  * X-Linear-Ticket and X-Mesh-Task-Id stamped when DispatchContext
    provides them; omitted when absent.
  * `_derive_gateway_base` strips a trailing `/v1` off `ollama_url` so a
    minimal env (only OLLAMA_URL set) still funnels through the gateway.
  * `_peek_linear_ticket` in main.py extracts SAL-xxxx from task title
    or description.

Uses a mock httpx.AsyncClient — the real gateway is not required.
"""

from __future__ import annotations

import json

import httpx
import pytest

from alfred_coo.dispatch import (
    Dispatcher,
    DispatchContext,
    _derive_gateway_base,
)


# ── Test doubles ────────────────────────────────────────────────────────


class _RecordingTransport(httpx.AsyncBaseTransport):
    """httpx mock transport that records every request and returns a canned
    OpenAI-compat response with usage stats.

    Also supports returning a response with `tool_calls` on first call so
    the tool-loop path can be exercised.
    """

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        # Each response is a dict (JSON body). Advances through the list per
        # call; once exhausted, returns the final dict forever (so fallback
        # loops don't crash).
        self._responses = responses or [_plain_response("ok")]
        self._idx = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._idx < len(self._responses):
            body = self._responses[self._idx]
            self._idx += 1
        else:
            body = self._responses[-1]
        return httpx.Response(200, json=body, request=request)


def _plain_response(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 7},
    }


def _install_mock_transport(monkeypatch, transport: _RecordingTransport) -> None:
    """Monkey-patch httpx.AsyncClient so every `async with httpx.AsyncClient(...)`
    inside dispatch.py picks up our recording transport.
    """
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ── _derive_gateway_base ────────────────────────────────────────────────


def test_derive_gateway_base_explicit_wins():
    assert _derive_gateway_base("http://example/", "http://other/v1") == "http://example"


def test_derive_gateway_base_strips_v1_suffix():
    assert _derive_gateway_base("", "http://172.17.0.1:8185/v1") == "http://172.17.0.1:8185"
    assert _derive_gateway_base("", "http://172.17.0.1:8185/v1/") == "http://172.17.0.1:8185"


def test_derive_gateway_base_no_v1_suffix_passthrough():
    assert _derive_gateway_base("", "http://172.17.0.1:8185") == "http://172.17.0.1:8185"


# ── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def dispatcher():
    return Dispatcher(
        ollama_url="http://172.17.0.1:8185/v1",
        anthropic_key="unused-post-ab21",
        openrouter_key="unused-post-ab21",
        gateway_url="http://gateway:8185",
        autobuild_soulkey="sk_autobuild_testhash",
        tiresias_tenant="alfred-coo-mc",
    )


@pytest.fixture
def ctx():
    return DispatchContext(
        persona="alfred-coo-a",
        linear_ticket="SAL-2710",
        mesh_task_id="task-uuid-abc",
    )


# ── Unified routing: all three families hit gateway ─────────────────────


@pytest.mark.parametrize(
    "model,expected_model_in_body",
    [
        ("claude-sonnet-4-7", "claude-sonnet-4-7"),
        ("qwen3-coder:480b-cloud", "qwen3-coder:480b-cloud"),
        ("deepseek-v3.2:cloud", "deepseek-v3.2:cloud"),
        ("llama3.1:70b-cloud", "llama3.1:70b-cloud"),
        ("openrouter/mistralai/mistral-large", "openrouter/mistralai/mistral-large"),
    ],
)
@pytest.mark.asyncio
async def test_all_model_families_funnel_through_gateway(
    monkeypatch, dispatcher, ctx, model, expected_model_in_body,
):
    transport = _RecordingTransport()
    _install_mock_transport(monkeypatch, transport)

    result = await dispatcher.call(
        model=model,
        system="you are a test",
        prompt="hello",
        context=ctx,
    )

    assert len(transport.requests) == 1
    req = transport.requests[0]
    # Every family MUST hit the gateway endpoint — no Anthropic-direct,
    # no openrouter.ai direct.
    assert str(req.url) == "http://gateway:8185/v1/chat/completions", str(req.url)
    body = json.loads(req.content)
    assert body["model"] == expected_model_in_body
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    assert result["content"] == "ok"
    assert result["tokens_in"] == 42
    assert result["tokens_out"] == 7
    assert result["model_used"] == model


# ── Header stamping ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_headers_stamped_with_full_context(monkeypatch, dispatcher, ctx):
    transport = _RecordingTransport()
    _install_mock_transport(monkeypatch, transport)

    await dispatcher.call("deepseek-v3.2:cloud", "sys", "prompt", context=ctx)

    req = transport.requests[0]
    assert req.headers["X-Tiresias-Tenant"] == "alfred-coo-mc"
    assert req.headers["X-Alfred-Persona"] == "alfred-coo-a"
    assert req.headers["X-Linear-Ticket"] == "SAL-2710"
    assert req.headers["X-Mesh-Task-Id"] == "task-uuid-abc"
    assert req.headers["Authorization"] == "Bearer sk_autobuild_testhash"
    assert req.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_optional_headers_omitted_when_context_fields_none(
    monkeypatch, dispatcher,
):
    transport = _RecordingTransport()
    _install_mock_transport(monkeypatch, transport)
    ctx = DispatchContext(persona="alfred-coo-a")  # no ticket, no mesh id

    await dispatcher.call("deepseek-v3.2:cloud", "sys", "prompt", context=ctx)

    headers = transport.requests[0].headers
    assert "X-Linear-Ticket" not in headers
    assert "X-Mesh-Task-Id" not in headers
    assert headers["X-Alfred-Persona"] == "alfred-coo-a"
    assert headers["X-Tiresias-Tenant"] == "alfred-coo-mc"


@pytest.mark.asyncio
async def test_authorization_omitted_when_soulkey_empty_does_not_crash(
    monkeypatch, caplog,
):
    """Empty soulkey must log a warning (at construction) but NOT crash, and
    must NOT stamp Authorization."""
    import logging
    with caplog.at_level(logging.WARNING, logger="alfred_coo.dispatch"):
        d = Dispatcher(
            ollama_url="http://gw/v1",
            gateway_url="http://gw",
            autobuild_soulkey="",  # empty
        )
    assert any("AUTOBUILD_SOULKEY" in r.message for r in caplog.records)

    transport = _RecordingTransport()
    _install_mock_transport(monkeypatch, transport)
    await d.call(
        "qwen3-coder:480b-cloud", "sys", "prompt",
        context=DispatchContext(persona="test"),
    )
    headers = transport.requests[0].headers
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_missing_context_uses_unknown_and_warns(monkeypatch, dispatcher, caplog):
    import logging
    # Reset the module-level warning flag so the warning fires in this test.
    import alfred_coo.dispatch as _d
    monkeypatch.setattr(_d, "_warned_missing_context", False)

    transport = _RecordingTransport()
    _install_mock_transport(monkeypatch, transport)

    with caplog.at_level(logging.WARNING, logger="alfred_coo.dispatch"):
        await dispatcher.call("claude-sonnet-4-7", "sys", "prompt")  # no context

    assert transport.requests[0].headers["X-Alfred-Persona"] == "unknown"
    assert any("without DispatchContext" in r.message for r in caplog.records)


# ── Tenant override ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_custom_tiresias_tenant_propagates(monkeypatch, ctx):
    d = Dispatcher(
        ollama_url="http://gw/v1",
        gateway_url="http://gw",
        autobuild_soulkey="sk",
        tiresias_tenant="alfred-coo-staging",
    )
    transport = _RecordingTransport()
    _install_mock_transport(monkeypatch, transport)

    await d.call("deepseek-v3.2:cloud", "sys", "prompt", context=ctx)

    assert transport.requests[0].headers["X-Tiresias-Tenant"] == "alfred-coo-staging"


# ── Fallback + call_with_tools also go through gateway ──────────────────


@pytest.mark.asyncio
async def test_fallback_call_also_hits_gateway(monkeypatch, ctx):
    """A non-retryable 4xx on primary triggers the fallback layer; both
    requests land on the gateway.

    AB-17-t (2026-04-24): switched the failure injection from 500 → 400 so
    the retry wrapper doesn't swallow this scenario. 5xx is now retried up
    to 3 times on the SAME model before fallback engages — see the AB-17-t
    test block at the bottom of this file for the 5xx-exhaust-then-fallback
    coverage.
    """
    class FailingThenOk(httpx.AsyncBaseTransport):
        def __init__(self):
            self.requests: list[httpx.Request] = []
            self.count = 0

        async def handle_async_request(self, request):
            self.requests.append(request)
            self.count += 1
            if self.count == 1:
                # 4xx is a deterministic client error — wrapper does NOT retry,
                # fallback layer above it engages immediately.
                return httpx.Response(400, json={"error": "bad"}, request=request)
            return httpx.Response(200, json=_plain_response("fb-ok"), request=request)

    d = Dispatcher(
        ollama_url="http://gw/v1",
        gateway_url="http://gw",
        autobuild_soulkey="sk",
    )
    transport = FailingThenOk()
    _install_mock_transport(monkeypatch, transport)

    result = await d.call(
        "claude-sonnet-4-7", "sys", "prompt",
        fallback_model="deepseek-v3.2:cloud",
        context=ctx,
    )
    assert result["content"] == "fb-ok"
    assert "->" in result["model_used"]
    assert len(transport.requests) == 2
    for r in transport.requests:
        assert str(r.url) == "http://gw/v1/chat/completions"


@pytest.mark.asyncio
async def test_call_with_tools_stamps_headers_every_iteration(monkeypatch, dispatcher, ctx):
    from alfred_coo.tools import ToolSpec

    # First response asks for a tool call; second completes.
    responses = [
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "demo_tool",
                            "arguments": "{}",
                        },
                    }],
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        _plain_response("all done"),
    ]
    transport = _RecordingTransport(responses=responses)
    _install_mock_transport(monkeypatch, transport)

    async def _demo_handler(**kwargs) -> dict:
        return {"ok": True}

    tool = ToolSpec(
        name="demo_tool",
        description="demo",
        parameters={"type": "object", "properties": {}},
        handler=_demo_handler,
    )

    result = await dispatcher.call_with_tools(
        "qwen3-coder:480b-cloud", "sys", "prompt",
        tools=[tool], context=ctx,
    )

    # Both iterations hit the gateway with the same stamped headers.
    assert len(transport.requests) == 2
    for req in transport.requests:
        assert str(req.url) == "http://gateway:8185/v1/chat/completions"
        assert req.headers["X-Alfred-Persona"] == "alfred-coo-a"
        assert req.headers["X-Linear-Ticket"] == "SAL-2710"
        assert req.headers["X-Mesh-Task-Id"] == "task-uuid-abc"
        assert req.headers["Authorization"] == "Bearer sk_autobuild_testhash"
    assert result["content"] == "all done"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "demo_tool"


# ── main._peek_linear_ticket ────────────────────────────────────────────


def test_peek_linear_ticket_from_title():
    from alfred_coo.main import _peek_linear_ticket
    assert _peek_linear_ticket({"title": "[persona:alfred-coo-a] SAL-2710 do thing"}) == "SAL-2710"


def test_peek_linear_ticket_from_description():
    from alfred_coo.main import _peek_linear_ticket
    task = {"title": "no ticket here", "description": "ref SAL-1234 for context"}
    assert _peek_linear_ticket(task) == "SAL-1234"


def test_peek_linear_ticket_missing_returns_none():
    from alfred_coo.main import _peek_linear_ticket
    assert _peek_linear_ticket({"title": "", "description": ""}) is None
    assert _peek_linear_ticket({"title": "nothing"}) is None


def test_peek_linear_ticket_prefers_title_over_description():
    from alfred_coo.main import _peek_linear_ticket
    task = {
        "title": "SAL-9999 kickoff",
        "description": "see also SAL-0001",
    }
    assert _peek_linear_ticket(task) == "SAL-9999"


# ── AB-17-m: size-aware iteration caps ──────────────────────────────────


def test_iteration_cap_for_size_known_labels():
    """size-S/M/L each map to their documented cap."""
    from alfred_coo.dispatch import iteration_cap_for_size
    assert iteration_cap_for_size("size-s") == 12
    assert iteration_cap_for_size("size-m") == 16
    assert iteration_cap_for_size("size-l") == 20
    # Case-insensitive: the orchestrator emits `size-S` (upper) and Linear
    # can emit either form depending on label source.
    assert iteration_cap_for_size("SIZE-S") == 12
    assert iteration_cap_for_size("Size-L") == 20


def test_iteration_cap_for_size_unknown_defaults_to_12():
    """Unknown / None / empty labels collapse to the size-S cap (12).

    Conservative default: a mis-labelled ticket shouldn't leak the size-L
    budget by accident. 12 matches the AB-17-l behaviour pre-AB-17-m.
    """
    from alfred_coo.dispatch import iteration_cap_for_size
    assert iteration_cap_for_size(None) == 12
    assert iteration_cap_for_size("") == 12
    assert iteration_cap_for_size("xyz") == 12
    assert iteration_cap_for_size("size-xl") == 12  # not in the cap dict
    assert iteration_cap_for_size("size-xs") == 12  # not in the cap dict


def test_iteration_cap_for_size_ceiling_bounded(monkeypatch):
    """Even with a patched cap dict exceeding MAX_TOOL_ITERATIONS, the
    helper clamps at the module ceiling — defence in depth against a rogue
    label map edit.
    """
    import alfred_coo.dispatch as d

    # Temporarily tighten the ceiling to prove clamping works. We can't
    # easily patch the inline dict, but we CAN lower MAX_TOOL_ITERATIONS
    # and re-check that 20 (the size-L cap) gets squashed to the new ceiling.
    monkeypatch.setattr(d, "MAX_TOOL_ITERATIONS", 10)
    assert d.iteration_cap_for_size("size-l") == 10
    assert d.iteration_cap_for_size("size-m") == 10
    assert d.iteration_cap_for_size("size-s") == 10  # 12 > 10, clamped


@pytest.mark.asyncio
async def test_run_tool_loop_honours_max_iterations_override(
    monkeypatch, dispatcher, ctx, caplog,
):
    """When the caller passes `max_iterations=4`, the loop bails at 4 and the
    warning reports "(4)" not the module ceiling "(20)".

    Exercised via a mock transport that keeps returning tool_calls forever
    so the loop must hit the cap.
    """
    import logging as _logging
    from alfred_coo.tools import ToolSpec

    # Every response asks for another tool call → loop runs until cap.
    tool_call_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "function": {"name": "demo_tool", "arguments": "{}"},
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = _RecordingTransport(responses=[tool_call_response] * 30)
    _install_mock_transport(monkeypatch, transport)

    async def _demo_handler(**kwargs) -> dict:
        return {"ok": True}

    tool = ToolSpec(
        name="demo_tool",
        description="demo",
        parameters={"type": "object", "properties": {}},
        handler=_demo_handler,
    )

    with caplog.at_level(_logging.WARNING, logger="alfred_coo.dispatch"):
        result = await dispatcher.call_with_tools(
            "qwen3-coder:480b-cloud",
            "sys",
            "prompt",
            tools=[tool],
            context=ctx,
            max_iterations=4,
        )

    # Exactly 4 model calls — the cap was honoured.
    assert len(transport.requests) == 4
    assert result.get("truncated") is True
    assert result["iterations"] == 4
    # Warning must reflect the effective cap (4), not MAX_TOOL_ITERATIONS (20).
    warnings = [
        r.message for r in caplog.records
        if r.levelno == _logging.WARNING and "MAX_TOOL_ITERATIONS" in r.message
    ]
    assert warnings, "expected a MAX_TOOL_ITERATIONS warning"
    assert "(4)" in warnings[-1], warnings[-1]


@pytest.mark.asyncio
async def test_run_tool_loop_clamps_override_at_ceiling(
    monkeypatch, dispatcher, ctx, caplog,
):
    """A rogue caller passing max_iterations=999 must still be clamped at
    MAX_TOOL_ITERATIONS. Proven by seeing the warning report the ceiling,
    not the override.
    """
    import logging as _logging
    from alfred_coo.tools import ToolSpec
    from alfred_coo.dispatch import MAX_TOOL_ITERATIONS

    tool_call_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "function": {"name": "demo_tool", "arguments": "{}"},
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = _RecordingTransport(responses=[tool_call_response] * 1000)
    _install_mock_transport(monkeypatch, transport)

    async def _demo_handler(**kwargs) -> dict:
        return {"ok": True}

    tool = ToolSpec(
        name="demo_tool",
        description="demo",
        parameters={"type": "object", "properties": {}},
        handler=_demo_handler,
    )

    with caplog.at_level(_logging.WARNING, logger="alfred_coo.dispatch"):
        result = await dispatcher.call_with_tools(
            "qwen3-coder:480b-cloud",
            "sys",
            "prompt",
            tools=[tool],
            context=ctx,
            max_iterations=999,
        )

    assert len(transport.requests) == MAX_TOOL_ITERATIONS
    assert result["iterations"] == MAX_TOOL_ITERATIONS


# ── SAL-3781: silent_with_tools early-abort ─────────────────────────────


@pytest.mark.asyncio
async def test_silent_with_tools_aborts_on_repeated_http_get(
    monkeypatch, dispatcher, ctx, caplog,
):
    """gpt-oss looping http_get without ever calling propose_pr is the
    canonical silent_with_tools failure. The loop should abort early at
    the threshold (3 consecutive same-tool iterations as of SAL-3802)
    instead of running to MAX_TOOL_ITERATIONS, returning
    ``silent_with_tools=True`` so the orchestrator can route the failure
    into its retry/fallback logic.
    """
    import logging as _logging
    from alfred_coo.tools import ToolSpec
    from alfred_coo.dispatch import _SILENT_WITH_TOOLS_THRESHOLD

    http_get_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "function": {
                        "name": "http_get",
                        "arguments": "{\"url\": \"https://example.com\"}",
                    },
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = _RecordingTransport(responses=[http_get_response] * 30)
    _install_mock_transport(monkeypatch, transport)

    async def _http_get_handler(**kwargs) -> dict:
        return {"status": 200, "body": "..."}

    tool = ToolSpec(
        name="http_get",
        description="fetch URL",
        parameters={"type": "object", "properties": {}},
        handler=_http_get_handler,
    )

    with caplog.at_level(_logging.WARNING, logger="alfred_coo.dispatch"):
        result = await dispatcher.call_with_tools(
            "gpt-oss:120b-cloud",
            "sys",
            "prompt",
            tools=[tool],
            context=ctx,
            max_iterations=20,
        )

    assert result.get("silent_with_tools") is True
    assert result.get("silent_with_tools_tool") == "http_get"
    assert result["iterations"] == _SILENT_WITH_TOOLS_THRESHOLD
    assert len(transport.requests) == _SILENT_WITH_TOOLS_THRESHOLD
    warnings = [
        r.message for r in caplog.records
        if r.levelno == _logging.WARNING and "silent_with_tools" in r.message
    ]
    assert warnings, "expected a silent_with_tools warning"


@pytest.mark.asyncio
async def test_silent_with_tools_does_not_fire_for_distinct_args_verification(
    monkeypatch, dispatcher, ctx,
):
    """SAL-3802 args-aware: 3 http_get calls to 3 DIFFERENT URLs is
    legitimate path verification (Step 2 of alfred-coo-a persona), not a
    loop. silent_with_tools must NOT trip when args are all distinct.

    Regression guard: pre-args-aware behavior counted any 3 consecutive
    same-tool calls as a loop, blocking builders from verifying multiple
    new_paths.
    """
    from alfred_coo.tools import ToolSpec

    def _http_get_response(url: str) -> dict:
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call_{url[-1]}",
                        "function": {
                            "name": "http_get",
                            "arguments": json.dumps({"url": url}),
                        },
                    }],
                }
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    # 5 distinct URLs followed by an empty-tool_calls response so the loop
    # exits cleanly (mimics builder finishing verification then committing
    # to a textual response — not propose_pr but also not a loop).
    transport = _RecordingTransport(responses=[
        _http_get_response("https://a/1"),
        _http_get_response("https://a/2"),
        _http_get_response("https://a/3"),
        _http_get_response("https://a/4"),
        _http_get_response("https://a/5"),
        _plain_response("verified all paths"),
    ])
    _install_mock_transport(monkeypatch, transport)

    async def _handler(**kwargs) -> dict:
        return {"status": 200, "body": "..."}

    tool = ToolSpec(
        name="http_get", description="x",
        parameters={"type": "object", "properties": {}}, handler=_handler,
    )

    result = await dispatcher.call_with_tools(
        "gpt-oss:120b-cloud", "sys", "prompt",
        tools=[tool], context=ctx, max_iterations=10,
    )

    # 5 distinct-URL fetches must NOT trip silent_with_tools.
    assert result.get("silent_with_tools") is not True
    assert result["content"] == "verified all paths"


@pytest.mark.asyncio
async def test_silent_with_tools_fires_when_args_repeat_within_run(
    monkeypatch, dispatcher, ctx, caplog,
):
    """SAL-3802 args-aware: even 3 distinct URL fetches followed by a
    repeat trips silent_with_tools — the duplicate signals a loop pattern
    (e.g., builder oscillating between two URLs hoping for clarity).
    """
    import logging as _logging
    from alfred_coo.tools import ToolSpec

    def _r(url: str) -> dict:
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_x",
                        "function": {
                            "name": "http_get",
                            "arguments": json.dumps({"url": url}),
                        },
                    }],
                }
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    # A, B, A — at iteration 3 we have count=3 (>= threshold 3) AND a
    # duplicate (A repeats). Should trip.
    transport = _RecordingTransport(responses=[
        _r("https://a/1"),
        _r("https://a/2"),
        _r("https://a/1"),
        _r("https://a/9"),  # tail, shouldn't be reached
    ])
    _install_mock_transport(monkeypatch, transport)

    async def _handler(**kwargs) -> dict:
        return {"status": 200, "body": "..."}

    tool = ToolSpec(
        name="http_get", description="x",
        parameters={"type": "object", "properties": {}}, handler=_handler,
    )

    with caplog.at_level(_logging.WARNING, logger="alfred_coo.dispatch"):
        result = await dispatcher.call_with_tools(
            "gpt-oss:120b-cloud", "sys", "prompt",
            tools=[tool], context=ctx, max_iterations=10,
        )

    assert result.get("silent_with_tools") is True
    assert result.get("silent_with_tools_tool") == "http_get"
    # Tripped on iteration 3 (the repeat), not later.
    assert result["iterations"] == 3


@pytest.mark.asyncio
async def test_silent_with_tools_does_not_fire_when_terminal_tool_called(
    monkeypatch, dispatcher, ctx,
):
    """If propose_pr fires at any point, silent_with_tools detection must
    stay disabled for the rest of the dispatch — even if http_get loops
    afterward (legitimate post-propose investigation).
    """
    from alfred_coo.tools import ToolSpec

    propose_pr_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_p",
                    "function": {
                        "name": "propose_pr",
                        "arguments": "{}",
                    },
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    http_get_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_h",
                    "function": {
                        "name": "http_get",
                        "arguments": "{}",
                    },
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = _RecordingTransport(
        responses=[propose_pr_response] + [http_get_response] * 10
    )
    _install_mock_transport(monkeypatch, transport)

    async def _handler(**kwargs) -> dict:
        return {"ok": True}

    tools = [
        ToolSpec(name="http_get", description="x", parameters={"type": "object", "properties": {}}, handler=_handler),
        ToolSpec(name="propose_pr", description="x", parameters={"type": "object", "properties": {}}, handler=_handler),
    ]

    result = await dispatcher.call_with_tools(
        "gpt-oss:120b-cloud", "sys", "prompt",
        tools=tools, context=ctx, max_iterations=8,
    )

    # Should run to max_iterations (truncated) without silent_with_tools flag,
    # because propose_pr fired in iteration 1.
    assert result.get("silent_with_tools") is not True
    assert result["iterations"] == 8


@pytest.mark.asyncio
async def test_silent_with_tools_does_not_fire_for_non_loop_risk_tool(
    monkeypatch, dispatcher, ctx,
):
    """A tool not in `_NONTERMINAL_LOOP_RISK_TOOLS` (currently only http_get
    is in the set) must NOT trigger silent_with_tools, even if called in a
    long loop — that's the existing test_run_tool_loop contract on
    `demo_tool`. This test guards against accidental over-broadening.
    """
    from alfred_coo.tools import ToolSpec

    demo_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_d",
                    "function": {"name": "demo_tool", "arguments": "{}"},
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = _RecordingTransport(responses=[demo_response] * 30)
    _install_mock_transport(monkeypatch, transport)

    async def _handler(**kwargs) -> dict:
        return {"ok": True}

    tool = ToolSpec(
        name="demo_tool", description="x",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )

    result = await dispatcher.call_with_tools(
        "qwen3-coder:480b-cloud", "sys", "prompt",
        tools=[tool], context=ctx, max_iterations=6,
    )

    assert result.get("silent_with_tools") is not True
    assert result["iterations"] == 6
    assert result.get("truncated") is True


# ── main._peek_size_label + _builder_iteration_cap ──────────────────────


def test_peek_size_label_from_size_line():
    """Orchestrator writes `Size: M` into the child task body."""
    from alfred_coo.main import _peek_size_label
    task = {
        "title": "[persona:alfred-coo-a] SAL-9001 — scaffold thing",
        "description": "Ticket: SAL-9001\nWave: 0\nSize: M\nEstimate: 5\n",
    }
    assert _peek_size_label(task) == "size-m"


def test_peek_size_label_from_label_tag_in_title():
    """Fallback: `size-L` tag embedded in title when body lacks Size: line."""
    from alfred_coo.main import _peek_size_label
    task = {
        "title": "[persona:alfred-coo-a] [size-L] SAL-9002 — refactor thing",
        "description": "no size line here",
    }
    assert _peek_size_label(task) == "size-l"


def test_peek_size_label_missing_returns_none():
    from alfred_coo.main import _peek_size_label
    assert _peek_size_label({"title": "", "description": ""}) is None
    assert _peek_size_label({"title": "no size info anywhere"}) is None


def test_builder_iteration_cap_size_l():
    """Builder persona + size-L task → 20-turn cap."""
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "x", "description": "Ticket: SAL-1\nSize: L\n"}
    assert _builder_iteration_cap("alfred-coo-a", task) == 20


def test_builder_iteration_cap_size_m():
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "x", "description": "Ticket: SAL-1\nSize: M\n"}
    assert _builder_iteration_cap("alfred-coo-a", task) == 16


def test_builder_iteration_cap_size_s():
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "x", "description": "Ticket: SAL-1\nSize: S\n"}
    assert _builder_iteration_cap("alfred-coo-a", task) == 12


def test_builder_iteration_cap_unknown_size_defaults_to_12():
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "no size", "description": "none here"}
    # Builder, but no size → default cap (12). Not None — the helper still
    # stamps the builder with a cap so the log line fires.
    assert _builder_iteration_cap("alfred-coo-a", task) == 12


def test_builder_iteration_cap_autonomous_build_a_also_gets_cap():
    """autonomous-build-a is the orchestrator persona and ALSO counts as a
    builder for AB-17-m purposes."""
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "x", "description": "Size: L\n"}
    assert _builder_iteration_cap("autonomous-build-a", task) == 20


def test_builder_iteration_cap_reviewer_returns_none():
    """hawkman-qa-a is the reviewer — MUST get None so the default cap is
    used. Blanket 20-turn budget on short review loops would just inflate
    cost on noisy misfires.
    """
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "review", "description": "Size: L\n"}  # size ignored
    assert _builder_iteration_cap("hawkman-qa-a", task) is None


def test_builder_iteration_cap_unknown_persona_returns_none():
    """Any persona not on the builder allowlist falls back to default cap."""
    from alfred_coo.main import _builder_iteration_cap
    task = {"title": "x", "description": "Size: L\n"}
    assert _builder_iteration_cap("some-future-persona", task) is None
    assert _builder_iteration_cap("unknown", task) is None


# ── SAL-2978: fix-round cap bump + iteration_count reset ────────────────


@pytest.mark.parametrize("label", ["size-s", "size-m", "size-l", None, "", "xyz"])
def test_iteration_cap_for_dispatch_initial_matches_size(label):
    """SAL-2978: with `is_fix_round=False` the new helper returns the
    unchanged size-based cap so existing call sites are unaffected.
    """
    from alfred_coo.dispatch import (
        iteration_cap_for_dispatch,
        iteration_cap_for_size,
    )
    assert iteration_cap_for_dispatch(label, is_fix_round=False) == \
        iteration_cap_for_size(label)


@pytest.mark.parametrize("label,expected", [
    ("size-s", 16),   # 12 + 4
    ("size-m", 20),   # 16 + 4 (= MAX_TOOL_ITERATIONS ceiling)
    ("size-l", 20),   # 20 + 4 = 24, clamped at ceiling
    (None, 16),       # default 12 + 4
    ("xyz", 16),
])
def test_iteration_cap_for_dispatch_fix_round_bump(label, expected):
    """SAL-2978: fix-round adds +4 over the size-based cap, clamped at
    MAX_TOOL_ITERATIONS.
    """
    from alfred_coo.dispatch import iteration_cap_for_dispatch
    assert iteration_cap_for_dispatch(label, is_fix_round=True) == expected


@pytest.mark.parametrize("title,expected", [
    # Live shape: em-dash marker emitted by `_respawn_for_fix_round`.
    ("[persona:alfred-coo-a] SAL-2588 TIR-06 — fix: round 1 (...)", True),
    # Forward-compat: ASCII fallback.
    ("[persona:alfred-coo-a] SAL-1 - fix: round 2 (...)", True),
    # Initial dispatch — no marker.
    ("[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2588 TIR-06", False),
    # Defensive empty.
    ("", False),
])
def test_is_fix_round_dispatch_classification(title, expected):
    """SAL-2978: detector matches live em-dash + ASCII shapes; rejects
    initial dispatches and empties.
    """
    from alfred_coo.main import _is_fix_round_dispatch
    assert _is_fix_round_dispatch({"title": title}) is expected


def test_fix_round_dispatch_bumps_iteration_cap():
    """SAL-2978 acceptance criterion: fix-round dispatches get bumped cap
    (size-S 12→16, size-M 16→20). Reproduces SAL-2588 TIR-06 fix: pre-
    SAL-2978 the same size-S task got 12 and exhausted it 3x in v7aa.
    """
    from alfred_coo.main import _builder_iteration_cap
    fix_s = {
        "title": "[persona:alfred-coo-a] SAL-2588 TIR-06 — fix: round 1 (...)",
        "description": "Size: S\n",
    }
    fix_m = {
        "title": "[persona:alfred-coo-a] SAL-9001 — fix: round 2 (...)",
        "description": "Size: M\n",
    }
    assert _builder_iteration_cap("alfred-coo-a", fix_s) == 16
    assert _builder_iteration_cap("alfred-coo-a", fix_m) == 20


def test_fix_round_does_not_bump_for_reviewer():
    """SAL-2978: reviewer still gets None — fix-round detection must NOT
    change the persona allowlist.
    """
    from alfred_coo.main import _builder_iteration_cap
    task = {
        "title": "[persona:hawkman-qa-a] review SAL-1 — fix: round 1 (...)",
        "description": "Size: S\n",
    }
    assert _builder_iteration_cap("hawkman-qa-a", task) is None


@pytest.mark.parametrize("size_letter,expected_cap", [("S", 12), ("M", 16), ("L", 20)])
def test_initial_dispatch_unchanged_by_fix_round_logic(size_letter, expected_cap):
    """SAL-2978 regression: initial dispatches (no fix-round marker) keep
    their legacy size-gated cap.
    """
    from alfred_coo.main import _builder_iteration_cap
    task = {
        "title": f"[persona:alfred-coo-a] SAL-1 — scaffold size-{size_letter}",
        "description": f"Size: {size_letter}\n",
    }
    assert _builder_iteration_cap("alfred-coo-a", task) == expected_cap


@pytest.mark.asyncio
async def test_iteration_count_resets_on_fresh_dispatch(
    monkeypatch, dispatcher, ctx, caplog,
):
    """SAL-2978 acceptance criterion: the iteration counter resets to 0 on
    every fresh dispatch. The counter is loop-local in `_tool_loop`
    (`for iteration in range(effective_cap)`), so two back-to-back
    dispatches must each report `iterations=N` against the SAME cap with
    no carryover. Also verifies the explicit
    `iteration_count_reset=True` log line fires on every dispatch.
    """
    import logging as _logging
    from alfred_coo.tools import ToolSpec

    # Each response asks for one tool call — the tool handler then returns
    # an "ok" envelope so the model emits a final message on iteration 2.
    # Two dispatches × 2 iterations each = 4 transport calls total.
    tool_call_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "function": {"name": "demo_tool", "arguments": "{}"},
                }],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    final_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "done",
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = _RecordingTransport(responses=[
        tool_call_response, final_response,  # dispatch 1: 2 iterations
        tool_call_response, final_response,  # dispatch 2: 2 iterations
    ])
    _install_mock_transport(monkeypatch, transport)

    async def _demo_handler(**kwargs) -> dict:
        return {"ok": True}

    tool = ToolSpec(
        name="demo_tool",
        description="demo",
        parameters={"type": "object", "properties": {}},
        handler=_demo_handler,
    )

    with caplog.at_level(_logging.INFO, logger="alfred_coo.dispatch"):
        result1 = await dispatcher.call_with_tools(
            "qwen3-coder:480b-cloud",
            "sys", "prompt 1",
            tools=[tool],
            context=ctx,
            max_iterations=12,
        )
        result2 = await dispatcher.call_with_tools(
            "qwen3-coder:480b-cloud",
            "sys", "prompt 2",
            tools=[tool],
            context=ctx,
            max_iterations=12,
        )

    # Both dispatches counted iterations from 0 — neither reports 4
    # (which would be the case if the counter leaked across dispatches).
    assert result1["iterations"] == 2
    assert result2["iterations"] == 2

    # The explicit reset log line fired once per dispatch.
    reset_logs = [
        r for r in caplog.records
        if r.levelno == _logging.INFO
        and "iteration_count_reset=True" in r.message
        and "tool-use loop entering" in r.message
    ]
    assert len(reset_logs) == 2, (
        f"expected 2 reset log lines (one per dispatch); got "
        f"{len(reset_logs)}: {[r.message for r in reset_logs]}"
    )


# ── AB-17-t · dispatch 5xx retry wrapper ────────────────────────────────


class _ProgrammableTransport(httpx.AsyncBaseTransport):
    """httpx transport that returns a scripted sequence of (status, body) tuples.

    Used by the AB-17-t tests to simulate 500-then-200 flap patterns. Once the
    sequence is exhausted, the final tuple repeats forever so misconfigured
    fallback loops don't crash the test.
    """

    def __init__(self, sequence: list[tuple[int, dict]]) -> None:
        self.sequence = sequence
        self.requests: list[httpx.Request] = []
        self._idx = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        idx = min(self._idx, len(self.sequence) - 1)
        self._idx += 1
        status, body = self.sequence[idx]
        return httpx.Response(status, json=body, request=request)


@pytest.fixture
def fast_retry(monkeypatch):
    """Strip the backoff delay so retry tests run in ~ms instead of seconds.

    The wrapper uses tenacity's `wait_exponential_jitter`; collapsing the
    initial+max to ~0 keeps the retry semantics intact (still 3 attempts,
    still gated on `_is_retryable_infra_error`) without forcing the suite to
    sleep through real exponential delays.
    """
    import alfred_coo.dispatch as d

    monkeypatch.setattr(d, "_INFRA_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(d, "_INFRA_RETRY_MAX_SECONDS", 0.002)


@pytest.mark.asyncio
async def test_5xx_retry_succeeds_on_third_attempt(monkeypatch, dispatcher, ctx, fast_retry):
    """Two 500s then a 200 — call must succeed at attempt 3 inside the same
    `_call_gateway`, before the fallback layer ever sees the failure."""
    transport = _ProgrammableTransport([
        (500, {"error": "upstream flap"}),
        (500, {"error": "upstream flap"}),
        (200, _plain_response("third-time-lucky")),
    ])
    _install_mock_transport(monkeypatch, transport)

    result = await dispatcher.call(
        "qwen3-coder:480b-cloud", "sys", "prompt",
        fallback_model="deepseek-v3.2:cloud",  # MUST NOT trigger
        context=ctx,
    )

    assert result["content"] == "third-time-lucky"
    # Same model used — fallback never engaged.
    assert result["model_used"] == "qwen3-coder:480b-cloud"
    assert "->" not in result["model_used"]
    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_4xx_does_not_retry(monkeypatch, dispatcher, ctx, fast_retry):
    """A 400 is a real client error — must surface fast, no retry."""
    transport = _ProgrammableTransport([
        (400, {"error": "bad request"}),
        # If the wrapper retries, it would hit a 200 here and succeed —
        # which would be a regression.
        (200, _plain_response("WRONG-should-not-reach")),
    ])
    _install_mock_transport(monkeypatch, transport)

    # 4xx should propagate; existing fallback layer in `call` then swaps
    # models. We pin fallback == primary so the propagated error escapes the
    # whole `call` and we can assert on it directly.
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await dispatcher.call(
            "qwen3-coder:480b-cloud", "sys", "prompt",
            fallback_model="qwen3-coder:480b-cloud",
            context=ctx,
        )
    assert excinfo.value.response.status_code == 400
    # Exactly ONE request — no retry on 4xx.
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_5xx_exhaustion_propagates_then_fallback_engages(
    monkeypatch, ctx, fast_retry,
):
    """Three 500s on primary → wrapper exhausts → existing fallback layer
    picks up and retries on a different model. Validates the retry sits
    BELOW the fallback (additive, not replacement)."""
    # 3 * 500 for primary, then 200 for fallback's first try.
    transport = _ProgrammableTransport([
        (500, {"error": "boom"}),
        (500, {"error": "boom"}),
        (500, {"error": "boom"}),
        (200, _plain_response("fallback-rescued")),
    ])
    d = Dispatcher(
        ollama_url="http://gw/v1",
        gateway_url="http://gw",
        autobuild_soulkey="sk",
    )
    _install_mock_transport(monkeypatch, transport)

    result = await d.call(
        "qwen3-coder:480b-cloud", "sys", "prompt",
        fallback_model="deepseek-v3.2:cloud",
        context=ctx,
    )

    assert result["content"] == "fallback-rescued"
    assert "->" in result["model_used"]  # fallback chain engaged
    # 3 retries on primary + 1 fallback call = 4 requests total.
    assert len(transport.requests) == 4


@pytest.mark.asyncio
async def test_retry_emits_infra_retry_log(monkeypatch, dispatcher, ctx, caplog, fast_retry):
    """Each retry attempt past the first emits an `[infra_retry]` warning so
    operators can spot upstream flaps in the log without parsing exception
    chains."""
    import logging as _logging

    transport = _ProgrammableTransport([
        (500, {"error": "flap"}),
        (200, _plain_response("ok")),
    ])
    _install_mock_transport(monkeypatch, transport)

    with caplog.at_level(_logging.WARNING, logger="alfred_coo.dispatch"):
        await dispatcher.call("qwen3-coder:480b-cloud", "sys", "prompt", context=ctx)

    retry_warnings = [r for r in caplog.records if "[infra_retry]" in r.message]
    assert retry_warnings, "expected at least one [infra_retry] log line"
    # The log must surface the URL and attempt number for ops triage.
    msg = retry_warnings[0].message
    assert "/v1/chat/completions" in msg
    assert "attempt=2" in msg
    assert "qwen3-coder:480b-cloud" in msg


def test_is_retryable_infra_error_classification():
    """Direct unit test on the predicate so the retry boundary is locked
    even if the AsyncRetrying loop is later refactored."""
    from alfred_coo.dispatch import _is_retryable_infra_error

    # Build minimal HTTPStatusError instances for the matrix.
    req = httpx.Request("POST", "http://gw/v1/chat/completions")

    err_500 = httpx.HTTPStatusError(
        "boom", request=req,
        response=httpx.Response(500, request=req),
    )
    err_503 = httpx.HTTPStatusError(
        "boom", request=req,
        response=httpx.Response(503, request=req),
    )
    err_400 = httpx.HTTPStatusError(
        "bad", request=req,
        response=httpx.Response(400, request=req),
    )
    err_404 = httpx.HTTPStatusError(
        "missing", request=req,
        response=httpx.Response(404, request=req),
    )

    assert _is_retryable_infra_error(err_500) is True
    assert _is_retryable_infra_error(err_503) is True
    assert _is_retryable_infra_error(err_400) is False
    assert _is_retryable_infra_error(err_404) is False

    # Connection-class errors retry.
    assert _is_retryable_infra_error(httpx.ConnectError("refused")) is True
    assert _is_retryable_infra_error(httpx.ReadTimeout("slow")) is True
    assert _is_retryable_infra_error(httpx.RemoteProtocolError("eof")) is True

    # Logic bugs do NOT retry.
    assert _is_retryable_infra_error(ValueError("oops")) is False
    assert _is_retryable_infra_error(KeyError("missing")) is False


# ── Sub #62: select_model registry integration ────────────────────────────


class _MiniPersona:
    """Stand-in persona for select_model tests."""
    def __init__(self, name: str, preferred: str | None = "deepseek-v3.2:cloud"):
        self.name = name
        self.preferred_model = preferred


def test_select_model_kickoff_override_wins(tmp_path, monkeypatch):
    """A `model_routing.<role>` field on the task overrides registry + tag."""
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    # Point registry at a temp file with builder=qwen3-coder.
    p = tmp_path / "registry.yaml"
    p.write_text(
        "schema_version: 1\n"
        "models:\n  qwen3-coder:480b-cloud: {provider: x, capabilities: [], status: active}\n"
        "  kimi-k2-thinking:cloud: {provider: x, capabilities: [], status: active}\n"
        "  gpt-oss:120b-cloud: {provider: x, capabilities: [], status: active}\n"
        "roles:\n"
        "  builder:\n"
        "    primary: qwen3-coder:480b-cloud\n"
        "    fallback_chain: []\n"
        "    last_resort: gpt-oss:120b-cloud\n"
        "stable_baseline:\n  builder: gpt-oss:120b-cloud\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_REGISTRY_PATH", str(p))
    mr._reset_for_tests()

    # Registry says qwen3-coder; kickoff override pins kimi.
    task = {
        "title": "[persona:alfred-coo-a] [tag:code] SAL-1 hello",
        "model_routing": {"builder": "kimi-k2-thinking:cloud"},
    }
    persona = _MiniPersona("alfred-coo-a")
    pick = select_model(task, persona)
    assert pick == "kimi-k2-thinking:cloud"

    # Without override the registry primary wins (NOT the legacy [tag:code]
    # path — registry takes precedence over tag for mapped personas).
    task_no_override = {"title": "[persona:alfred-coo-a] SAL-1 hello"}
    pick2 = select_model(task_no_override, persona)
    assert pick2 == "qwen3-coder:480b-cloud"
    mr._reset_for_tests()


def test_select_model_legacy_tag_still_works_for_unmapped_persona(monkeypatch, tmp_path):
    """Personas not in `_PERSONA_ROLE_MAP` keep legacy tag-based routing."""
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setattr(
        mr, "_DEFAULT_REGISTRY_PATHS",
        [str(tmp_path / "x.yaml"), str(tmp_path / "y.yaml")],
    )
    monkeypatch.delenv("MODEL_REGISTRY_PATH", raising=False)
    mr._reset_for_tests()

    # Persona name that's NOT in _PERSONA_ROLE_MAP; legacy tag route applies.
    task = {"title": "[persona:default] [tag:code] hello"}
    persona = _MiniPersona("default")
    assert select_model(task, persona) == "qwen3-coder:480b-cloud"

    # SAL-3787: tag:strategy used to hardcode-return deepseek-v3.2:cloud.
    # Now routes through _resolve_safe_fallback; for an unmapped persona
    # (no entry in _PERSONA_ROLE_MAP) the resolver returns the static
    # safe default _SAFE_FALLBACK_DEFAULT = "gpt-oss:120b-cloud".
    task2 = {"title": "[persona:default] [tag:strategy] hello"}
    assert select_model(task2, persona) == "gpt-oss:120b-cloud"

    # No tag, no registry, no preferred -> safe-fallback default (was
    # the deepseek hardcode pre-SAL-3787).
    persona_no_pref = _MiniPersona("default", preferred=None)
    assert select_model({"title": "[persona:default] x"}, persona_no_pref) == "gpt-oss:120b-cloud"


def test_select_model_registry_fallback_to_persona_preferred(monkeypatch, tmp_path):
    """Mapped persona but registry returns None for the role => persona.preferred_model."""
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    # Registry path doesn't exist -> _pick_model_for_role returns None.
    monkeypatch.setattr(
        mr, "_DEFAULT_REGISTRY_PATHS",
        [str(tmp_path / "x.yaml")],
    )
    monkeypatch.delenv("MODEL_REGISTRY_PATH", raising=False)
    mr._reset_for_tests()

    persona = _MiniPersona("alfred-coo-a", preferred="qwen3-coder:30b-a3b-q4_K_M")
    task = {"title": "[persona:alfred-coo-a] hello"}
    # No tag, no registry => fall back to persona.preferred_model.
    assert select_model(task, persona) == "qwen3-coder:30b-a3b-q4_K_M"


# ── SAL-3670: builder_fallback_chain[0] honored at attempt 0 ───────────────


def _registry_with_broken_baseline(tmp_path) -> str:
    """Write a registry whose builder primary is gpt-oss:120b-cloud — the
    real-world broken baseline that SAL-3670 protects against. Tests that
    rely on the chain winning over the registry use this fixture so the
    assertion proves the chain bypasses the broken pick.
    """
    p = tmp_path / "registry.yaml"
    p.write_text(
        "schema_version: 1\n"
        "models:\n"
        "  gpt-oss:120b-cloud: {provider: x, capabilities: [], status: active}\n"
        "  kimi-k2-thinking:cloud: {provider: x, capabilities: [], status: active}\n"
        "  qwen3-coder:480b-cloud: {provider: x, capabilities: [], status: active}\n"
        "roles:\n"
        "  builder:\n"
        "    primary: gpt-oss:120b-cloud\n"
        "    fallback_chain: []\n"
        "    last_resort: gpt-oss:120b-cloud\n"
        "  qa:\n"
        "    primary: gpt-oss:120b-cloud\n"
        "    fallback_chain: []\n"
        "    last_resort: gpt-oss:120b-cloud\n"
        "stable_baseline:\n  builder: gpt-oss:120b-cloud\n",
        encoding="utf-8",
    )
    return str(p)


def test_kickoff_fallback_chain_overrides_registry_for_builder_attempt_0(
    tmp_path, monkeypatch
):
    """SAL-3670: builder_fallback_chain[0] must beat registry at attempt 0.

    Bug context: when the kickoff payload supplies a fallback chain, the
    operator's first preference is the canonical attempt-0 model. Prior to
    this fix, dispatch ignored the chain and used registry.primary, which
    in production was the broken gpt-oss:120b-cloud baseline.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv("MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path))
    mr._reset_for_tests()

    payload = {
        "builder_fallback_chain": [
            "kimi-k2-thinking:cloud",
            "qwen3-coder:480b-cloud",
        ],
    }
    task = {
        "title": "[persona:alfred-coo-a] SAL-3670 hello",
        "description": json.dumps(payload),
    }
    persona = _MiniPersona("alfred-coo-a")
    assert select_model(task, persona) == "kimi-k2-thinking:cloud"
    mr._reset_for_tests()


def test_model_routing_beats_fallback_chain(tmp_path, monkeypatch):
    """`model_routing.builder` is a hard pin and must outrank the chain.

    The chain is a wishlist (preferred order); `model_routing.builder` is
    a per-kickoff override the operator sets when they want exactly one
    model. Override > chain > registry.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv("MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path))
    mr._reset_for_tests()

    payload = {
        "model_routing": {"builder": "qwen3-coder:480b-cloud"},
        "builder_fallback_chain": [
            "kimi-k2-thinking:cloud",
            "gpt-oss:120b-cloud",
        ],
    }
    task = {
        "title": "[persona:alfred-coo-a] SAL-3670 hello",
        "description": json.dumps(payload),
    }
    persona = _MiniPersona("alfred-coo-a")
    assert select_model(task, persona) == "qwen3-coder:480b-cloud"
    mr._reset_for_tests()


def test_fallback_chain_only_affects_builder_role(tmp_path, monkeypatch):
    """The chain is builder-only; QA/orchestrator must ignore it.

    QA selection has its own routing knobs (model_routing.qa, registry qa
    role). A `builder_fallback_chain` set on a QA task must NOT leak into
    the QA pick — otherwise QA would mirror builder choice, defeating the
    point of role-segmented routing.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv("MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path))
    mr._reset_for_tests()

    payload = {
        "builder_fallback_chain": [
            "kimi-k2-thinking:cloud",
            "qwen3-coder:480b-cloud",
        ],
    }
    task = {
        "title": "[persona:hawkman-qa-a] SAL-3670 review",
        "description": json.dumps(payload),
    }
    persona = _MiniPersona("hawkman-qa-a")
    # Chain is builder-only -> QA falls through to registry primary, which
    # in this fixture is gpt-oss:120b-cloud. The point: kimi was NOT picked.
    assert select_model(task, persona) == "gpt-oss:120b-cloud"
    mr._reset_for_tests()


def test_chain_in_child_task_dict_path(tmp_path, monkeypatch):
    """Orchestrator-injected child path: `task["builder_fallback_chain"]`.

    When the orchestrator builds child task dicts it sets the chain
    directly on the task object (no description JSON wrapper). Mirrors how
    `_peek_kickoff_model_override` handles the direct dict path for
    `model_routing`.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv("MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path))
    mr._reset_for_tests()

    task = {
        "title": "[persona:alfred-coo-a] SAL-3670 child",
        # Direct dict path — no description JSON.
        "builder_fallback_chain": [
            "kimi-k2-thinking:cloud",
            "qwen3-coder:480b-cloud",
        ],
    }
    persona = _MiniPersona("alfred-coo-a")
    assert select_model(task, persona) == "kimi-k2-thinking:cloud"
    mr._reset_for_tests()


# ── SAL-3670 follow-up: child-task body propagation block ──────────────────
#
# The 2026-04-30 follow-up closes the propagation gap: the kickoff payload's
# ``model_routing`` / ``builder_fallback_chain`` only fired for the
# orchestrator parent task itself; spawned child tasks inherited NEITHER
# field, so child ``select_model`` silently fell through to registry primary
# even when the kickoff pinned a chain head. The orchestrator now embeds a
# ``<!-- model_routing: {...} -->`` HTML-comment block at the top of every
# child body when the operator overrode the default chain or set
# ``model_routing``; ``_peek_kickoff_payload`` recognises that block and
# returns the same payload-shape the JSON envelope produces.


def test_chain_in_propagation_block_on_child_body(tmp_path, monkeypatch):
    """Propagation block on a child markdown body: ``select_model`` reads
    ``builder_fallback_chain[0]`` from the embedded JSON and uses it as the
    attempt-0 model.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv(
        "MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path),
    )
    mr._reset_for_tests()

    block = json.dumps({
        "builder_fallback_chain": [
            "kimi-k2-thinking:cloud",
            "qwen3-coder:480b-cloud",
        ],
    })
    body = (
        f"Ticket: SAL-1 (X)\n"
        f"<!-- model_routing: {block} -->\n"
        f"Wave: 0\nSize: S\n"
        f"## Deliverable\nOpen ONE PR.\n"
    )
    task = {
        "title": "[persona:alfred-coo-a] SAL-1",
        "description": body,
    }
    persona = _MiniPersona("alfred-coo-a")
    assert select_model(task, persona) == "kimi-k2-thinking:cloud"
    mr._reset_for_tests()


def test_model_routing_in_propagation_block_on_child_body(tmp_path, monkeypatch):
    """Symmetric coverage for the propagation block: when the orchestrator
    embeds ``model_routing.builder`` in the propagation block, child
    builder dispatch picks it up at attempt 0.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv(
        "MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path),
    )
    mr._reset_for_tests()

    block = json.dumps({
        "model_routing": {"builder": "qwen3-coder:480b-cloud"},
    })
    body = (
        f"Ticket: SAL-1 (X)\n"
        f"<!-- model_routing: {block} -->\n"
        f"## Deliverable\nx\n"
    )
    task = {
        "title": "[persona:alfred-coo-a] SAL-1",
        "description": body,
    }
    persona = _MiniPersona("alfred-coo-a")
    assert select_model(task, persona) == "qwen3-coder:480b-cloud"
    mr._reset_for_tests()


def test_propagation_block_malformed_json_falls_through(tmp_path, monkeypatch):
    """Malformed JSON inside the propagation marker must NOT crash
    ``select_model`` — falls through to the next precedence level (registry
    primary in this fixture).
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    monkeypatch.setenv(
        "MODEL_REGISTRY_PATH", _registry_with_broken_baseline(tmp_path),
    )
    mr._reset_for_tests()

    body = (
        "Ticket: SAL-1 (X)\n"
        "<!-- model_routing: {not valid json} -->\n"
        "## Deliverable\nx\n"
    )
    task = {
        "title": "[persona:alfred-coo-a] SAL-1",
        "description": body,
    }
    persona = _MiniPersona("alfred-coo-a")
    # Falls through to registry primary (gpt-oss:120b-cloud in fixture).
    assert select_model(task, persona) == "gpt-oss:120b-cloud"
    mr._reset_for_tests()


# ── _peek_kickoff_payload + helpers direct unit tests ──────────────────────


def test_peek_kickoff_payload_full_json_envelope():
    """Whole-description JSON parses cleanly (legacy parent kickoff)."""
    from alfred_coo.dispatch import _peek_kickoff_payload
    task = {
        "description": json.dumps({"linear_project_id": "p", "x": 1}),
    }
    parsed = _peek_kickoff_payload(task)
    assert parsed == {"linear_project_id": "p", "x": 1}


def test_peek_kickoff_payload_propagation_block():
    """Propagation block extracted from a markdown body."""
    from alfred_coo.dispatch import _peek_kickoff_payload
    body = (
        "Ticket: SAL-1\n"
        '<!-- model_routing: {"model_routing": {"builder": "X"}} -->\n'
        "## Deliverable\nx\n"
    )
    parsed = _peek_kickoff_payload({"description": body})
    assert parsed == {"model_routing": {"builder": "X"}}


def test_peek_kickoff_payload_returns_none_for_plain_body():
    """Plain markdown body with no JSON envelope or marker → None."""
    from alfred_coo.dispatch import _peek_kickoff_payload
    body = "Ticket: SAL-1\n## Deliverable\nx\n"
    assert _peek_kickoff_payload({"description": body}) is None
    assert _peek_kickoff_payload({}) is None
    assert _peek_kickoff_payload(None) is None  # type: ignore[arg-type]


def test_peek_kickoff_payload_handles_malformed_block():
    """Malformed JSON inside the block returns None, doesn't raise."""
    from alfred_coo.dispatch import _peek_kickoff_payload
    body = (
        "Ticket: SAL-1\n"
        "<!-- model_routing: {malformed} -->\n"
        "## Deliverable\nx\n"
    )
    assert _peek_kickoff_payload({"description": body}) is None


# ── SAL-3939: size-aware dispatch ─────────────────────────────────────────
#
# Acceptance contract (verbatim from PR body):
#   1. select_model("builder", size="S", payload={}) -> gpt-oss:120b-cloud, cap=4
#   2. select_model("builder", size="M", payload={}) -> qwen3-coder:480b-cloud, cap=8
#   3. select_model("builder", size="L", payload={}) -> kimi-k2-thinking:cloud, cap=12
#   4. select_model("builder", size="XL", payload={}) -> kimi-k2-thinking:cloud, cap=16
#   5. unknown / missing size -> kimi-k2-thinking:cloud, cap=12
#   6. model_routing.builder override -> wins over size-aware
#   7. model_routing.iteration_cap_by_size override -> wins over default mapping
#   8. The legacy two-arg select_model(task, persona) MUST still work (back-compat).


def test_size_aware_select_model_size_s():
    """size=S → gpt-oss primary, fallback qwen3-coder local, cap=4."""
    from alfred_coo.dispatch import select_model, SizeAwareDispatch

    pick = select_model("builder", size="S", payload={})
    assert isinstance(pick, SizeAwareDispatch)
    assert pick.model == "gpt-oss:120b-cloud"
    assert pick.iteration_cap == 4
    assert pick.source == "size-default"
    assert pick.cap_source == "size-default"
    assert pick.fallback_model == "qwen3-coder:30b-a3b-q4_K_M"


def test_size_aware_select_model_size_m():
    """size=M → qwen3-coder:480b primary, fallback gpt-oss, cap=8."""
    from alfred_coo.dispatch import select_model

    pick = select_model("builder", size="M", payload={})
    assert pick.model == "qwen3-coder:480b-cloud"
    assert pick.iteration_cap == 8
    assert pick.source == "size-default"
    assert pick.fallback_model == "gpt-oss:120b-cloud"


def test_size_aware_select_model_size_l():
    """size=L → kimi-k2-thinking primary, fallback qwen3-coder:480b, cap=12."""
    from alfred_coo.dispatch import select_model

    pick = select_model("builder", size="L", payload={})
    assert pick.model == "kimi-k2-thinking:cloud"
    assert pick.iteration_cap == 12
    assert pick.source == "size-default"
    assert pick.fallback_model == "qwen3-coder:480b-cloud"


def test_size_aware_select_model_size_xl():
    """size=XL → kimi-k2-thinking primary (same as L), cap=16."""
    from alfred_coo.dispatch import select_model

    pick = select_model("builder", size="XL", payload={})
    assert pick.model == "kimi-k2-thinking:cloud"
    assert pick.iteration_cap == 16
    assert pick.source == "size-default"
    assert pick.fallback_model == "qwen3-coder:480b-cloud"


def test_size_aware_select_model_unknown_size_falls_back_to_defaults():
    """Unknown / missing size → pre-3939 defaults (kimi-thinking, cap=12)."""
    from alfred_coo.dispatch import select_model

    for bad in (None, "", "size-xxl", "weird", "Q"):
        pick = select_model("builder", size=bad, payload={})
        assert pick.model == "kimi-k2-thinking:cloud", f"bad size: {bad!r}"
        assert pick.iteration_cap == 12, f"bad size: {bad!r}"
        assert pick.source == "registry-default", f"bad size: {bad!r}"
        assert pick.cap_source == "registry-default", f"bad size: {bad!r}"
        assert pick.fallback_model is None, f"bad size: {bad!r}"


def test_size_aware_select_model_no_payload():
    """Payload omitted entirely (None) — must not raise; falls through to defaults."""
    from alfred_coo.dispatch import select_model

    pick = select_model("builder", size="S", payload=None)
    assert pick.model == "gpt-oss:120b-cloud"
    assert pick.iteration_cap == 4
    assert pick.source == "size-default"


def test_size_aware_select_model_canonicalises_label_forms():
    """Accepts S / size-s / SIZE-S / size:s / lower / mixed case."""
    from alfred_coo.dispatch import select_model

    for label in ("S", "s", "size-s", "SIZE-S", "Size: S", "size:S"):
        pick = select_model("builder", size=label, payload={})
        assert pick.model == "gpt-oss:120b-cloud", f"label: {label!r}"
        assert pick.iteration_cap == 4, f"label: {label!r}"
    # XS collapses to S (orchestrator's ad-hoc XS label reuses size-S budget).
    pick_xs = select_model("builder", size="XS", payload={})
    assert pick_xs.model == "gpt-oss:120b-cloud"
    assert pick_xs.iteration_cap == 4


def test_size_aware_model_routing_override_wins():
    """`model_routing.builder` in payload pins the model regardless of size."""
    from alfred_coo.dispatch import select_model

    payload = {"model_routing": {"builder": "deepseek-v3.2:cloud"}}
    pick = select_model("builder", size="S", payload=payload)
    assert pick.model == "deepseek-v3.2:cloud"
    assert pick.source == "kickoff_override"
    # Cap is independent — no override on cap, so size-S default (4) holds.
    assert pick.iteration_cap == 4
    assert pick.cap_source == "size-default"


def test_size_aware_iteration_cap_by_size_override_wins():
    """`model_routing.iteration_cap_by_size[<size>]` overrides the cap default."""
    from alfred_coo.dispatch import select_model

    payload = {"model_routing": {"iteration_cap_by_size": {"S": 2}}}
    pick = select_model("builder", size="S", payload=payload)
    assert pick.iteration_cap == 2
    assert pick.cap_source == "kickoff_override"
    # Model fell through to size-default since no model override given.
    assert pick.model == "gpt-oss:120b-cloud"
    assert pick.source == "size-default"


def test_size_aware_iteration_cap_override_lowercase_key():
    """Override key matching is case-insensitive — 's' and 'S' both work."""
    from alfred_coo.dispatch import select_model

    payload = {"model_routing": {"iteration_cap_by_size": {"s": 6}}}
    pick = select_model("builder", size="S", payload=payload)
    assert pick.iteration_cap == 6
    assert pick.cap_source == "kickoff_override"


def test_size_aware_iteration_cap_override_full_table():
    """Operator can supply a full iteration_cap_by_size table at once."""
    from alfred_coo.dispatch import select_model

    cap_table = {"S": 2, "M": 4, "L": 6, "XL": 8}
    payload = {"model_routing": {"iteration_cap_by_size": cap_table}}
    expected = [("S", 2), ("M", 4), ("L", 6), ("XL", 8)]
    for size, cap in expected:
        pick = select_model("builder", size=size, payload=payload)
        assert pick.iteration_cap == cap, (size, pick)
        assert pick.cap_source == "kickoff_override"


def test_size_aware_iteration_cap_override_clamped_at_ceiling():
    """Even a deliberate 999 in the override is clamped at MAX_TOOL_ITERATIONS."""
    from alfred_coo.dispatch import select_model, MAX_TOOL_ITERATIONS

    payload = {"model_routing": {"iteration_cap_by_size": {"S": 999}}}
    pick = select_model("builder", size="S", payload=payload)
    assert pick.iteration_cap == MAX_TOOL_ITERATIONS


def test_size_aware_qa_role_uses_default_model_but_honours_cap_override():
    """Non-builder role: skip the size-aware MODEL table, but still honour
    the iteration_cap_by_size override and the model_routing override.
    """
    from alfred_coo.dispatch import select_model

    # No override → registry-default model, but cap is still picked from the
    # size table (size-aware caps apply to any role).
    pick = select_model("qa", size="L", payload={})
    assert pick.model == "kimi-k2-thinking:cloud"  # the registry-default
    assert pick.source == "registry-default"
    assert pick.iteration_cap == 12
    assert pick.cap_source == "size-default"

    # Explicit QA override wins for model.
    payload = {"model_routing": {"qa": "gpt-oss:120b-cloud"}}
    pick2 = select_model("qa", size="L", payload=payload)
    assert pick2.model == "gpt-oss:120b-cloud"
    assert pick2.source == "kickoff_override"


def test_size_aware_payload_propagation_via_description_envelope():
    """When payload is a task dict whose `description` is a JSON envelope,
    `model_routing` inside the envelope is honoured. This matches the legacy
    `_peek_kickoff_payload` contract — the size-aware path uses the same
    helper so the envelope works in both APIs.
    """
    from alfred_coo.dispatch import select_model

    envelope = json.dumps(
        {"model_routing": {"builder": "qwen3-coder:480b-cloud"}}
    )
    payload = {"description": envelope}
    pick = select_model("builder", size="S", payload=payload)
    assert pick.model == "qwen3-coder:480b-cloud"
    assert pick.source == "kickoff_override"


def test_size_aware_payload_propagation_via_html_comment_block():
    """The `<!-- model_routing: {...} -->` propagation block on a child task
    body is also honoured — same precedence as the JSON envelope.
    """
    from alfred_coo.dispatch import select_model

    body = (
        "Ticket: SAL-3939\n"
        '<!-- model_routing: '
        '{"model_routing": {"iteration_cap_by_size": {"M": 5}}} -->\n'
        "## Deliverable\nx\n"
    )
    payload = {"description": body}
    pick = select_model("builder", size="M", payload=payload)
    assert pick.iteration_cap == 5
    assert pick.cap_source == "kickoff_override"


def test_legacy_select_model_still_works_two_arg_form(tmp_path, monkeypatch):
    """Back-compat: select_model(task, persona) returns a string, not a
    SizeAwareDispatch. The polymorphic dispatcher must not break the legacy
    callsites in main.py / orchestrator.py.
    """
    from alfred_coo.dispatch import select_model
    from alfred_coo.autonomous_build import model_registry as mr

    # Registry pin the role so the legacy path returns a deterministic value.
    p = tmp_path / "registry.yaml"
    p.write_text(
        "schema_version: 1\n"
        "models:\n  qwen3-coder:480b-cloud: {provider: x, capabilities: [], status: active}\n"
        "  gpt-oss:120b-cloud: {provider: x, capabilities: [], status: active}\n"
        "roles:\n"
        "  builder:\n"
        "    primary: qwen3-coder:480b-cloud\n"
        "    fallback_chain: []\n"
        "    last_resort: gpt-oss:120b-cloud\n"
        "stable_baseline:\n  builder: qwen3-coder:480b-cloud\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_REGISTRY_PATH", str(p))
    mr._reset_for_tests()

    task = {"title": "[persona:alfred-coo-a] SAL-1 hello"}
    persona = _MiniPersona("alfred-coo-a")
    pick = select_model(task, persona)
    assert isinstance(pick, str)
    assert pick == "qwen3-coder:480b-cloud"
    mr._reset_for_tests()


def test_log_size_aware_dispatch_emits_canonical_line(caplog):
    """`log_size_aware_dispatch` emits a single INFO line containing the
    canonical (size, model, source, iteration_cap, cap_source) tuple. Tests
    in observability dashboards grep on this shape.
    """
    import logging
    from alfred_coo.dispatch import (
        log_size_aware_dispatch,
        SizeAwareDispatch,
    )

    pick = SizeAwareDispatch(
        model="gpt-oss:120b-cloud",
        iteration_cap=4,
        source="size-default",
        cap_source="size-default",
        fallback_model="qwen3-coder:30b-a3b-q4_K_M",
    )
    with caplog.at_level(logging.INFO, logger="alfred_coo.dispatch"):
        log_size_aware_dispatch(ticket="SAL-3939", size="S", pick=pick)
    # Exactly one matching record.
    relevant = [r for r in caplog.records if "dispatching SAL-3939" in r.message]
    assert len(relevant) == 1
    msg = relevant[0].message
    assert "size=size-s" in msg
    assert "model=gpt-oss:120b-cloud" in msg
    assert "source=size-default" in msg
    assert "iteration_cap=4" in msg
    assert "cap_source=size-default" in msg


def test_log_size_aware_dispatch_unknown_size_renders_unknown(caplog):
    """When size is None / unknown, the log line says size=unknown not None."""
    import logging
    from alfred_coo.dispatch import (
        log_size_aware_dispatch,
        SizeAwareDispatch,
    )

    pick = SizeAwareDispatch(
        model="kimi-k2-thinking:cloud",
        iteration_cap=12,
        source="registry-default",
        cap_source="registry-default",
    )
    with caplog.at_level(logging.INFO, logger="alfred_coo.dispatch"):
        log_size_aware_dispatch(ticket="SAL-3939", size=None, pick=pick)
    relevant = [r for r in caplog.records if "dispatching SAL-3939" in r.message]
    assert len(relevant) == 1
    assert "size=unknown" in relevant[0].message
