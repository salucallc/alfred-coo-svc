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
