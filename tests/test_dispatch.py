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
    """First call raises (simulated by 500 via transport with custom response);
    fallback retries with a different model; both requests land on the gateway.
    """
    class FailingThenOk(httpx.AsyncBaseTransport):
        def __init__(self):
            self.requests: list[httpx.Request] = []
            self.count = 0

        async def handle_async_request(self, request):
            self.requests.append(request)
            self.count += 1
            if self.count == 1:
                return httpx.Response(500, json={"error": "boom"}, request=request)
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
