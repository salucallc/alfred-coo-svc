"""
Model dispatcher with multi-provider fallback support.

Phase B.3.1 adds OpenAI-compatible tool-use (`call_with_tools`) for models that
support it (deepseek, qwen, kimi, llama via Ollama Cloud; any OpenRouter model
that advertises tool support). Tool-use is opt-in via the caller passing a
non-empty tools list.
"""

import json
import logging

import httpx
from anthropic import AsyncAnthropic

from .tools import ToolSpec, execute_tool, openai_tool_schema


logger = logging.getLogger("alfred_coo.dispatch")

MAX_TOOL_ITERATIONS = 8

def select_model(task: dict, persona) -> str:
    title = task.get("title", "")
    if "[tag:strategy]" in title:
        return "deepseek-v3.2:cloud"
    elif "[tag:code]" in title:
        return "qwen3-coder:480b-cloud"
    elif persona.preferred_model:
        return persona.preferred_model
    else:
        return "deepseek-v3.2:cloud"

class Dispatcher:
    def __init__(self, ollama_url: str, anthropic_key: str, openrouter_key: str, timeout: float = 300.0):
        self.ollama_url = ollama_url.rstrip("/")
        self.anthropic_client = AsyncAnthropic(api_key=anthropic_key)
        self.openrouter_key = openrouter_key
        self.timeout = timeout

    async def call(
        self,
        model: str,
        system: str,
        prompt: str,
        fallback_model: str | None = None,
    ) -> dict:
        try:
            return await self._call_model(model, system, prompt)
        except Exception:
            fb = fallback_model or "deepseek-v3.2:cloud"
            if fb == model:
                raise
            result = await self._call_model(fb, system, prompt)
            result["model_used"] = f"{model} -> {fb}"
            return result

    async def _call_model(self, model: str, system: str, prompt: str) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]

        if model.startswith("claude-"):
            response = await self.anthropic_client.messages.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                system=system,
                max_tokens=4096
            )
            content = response.content[0].text
            tokens_in = response.usage.input_tokens
            tokens_out = response.usage.output_tokens
        elif ":cloud" in model or model.startswith(("qwen", "deepseek", "llama")):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.ollama_url}/chat/completions",
                    json={"model": model, "messages": messages}
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                tokens_in = data.get("usage", {}).get("prompt_tokens", 0)
                tokens_out = data.get("usage", {}).get("completion_tokens", 0)
        elif model.startswith("openrouter/"):
            actual_model = model[len("openrouter/"):]
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.openrouter_key}"},
                    json={"model": actual_model, "messages": messages}
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                tokens_in = data.get("usage", {}).get("prompt_tokens", 0)
                tokens_out = data.get("usage", {}).get("completion_tokens", 0)
        else:
            raise ValueError(f"Unsupported model: {model}")

        return {
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model_used": model
        }

    async def call_with_tools(
        self,
        model: str,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        fallback_model: str | None = None,
    ) -> dict:
        """Multi-turn OpenAI-compatible tool-use loop.

        The model can emit tool_calls; each call is executed and its JSON result
        fed back as a role=tool message; the loop runs until the model emits a
        final message with no tool_calls, or MAX_TOOL_ITERATIONS is hit.

        Fallback: if the primary model errors at any point, retry the WHOLE loop
        against fallback_model with a fresh message history. This trades some
        wasted tokens for correctness — a partial tool chain on one model cannot
        be meaningfully resumed on another.
        """
        if not tools:
            return await self.call(model, system, prompt, fallback_model=fallback_model)

        try:
            return await self._tool_loop(model, system, prompt, tools)
        except Exception as e:
            fb = fallback_model or "deepseek-v3.2:cloud"
            if fb == model:
                raise
            logger.warning("tool-use primary %s failed (%s); retrying on %s", model, e, fb)
            result = await self._tool_loop(fb, system, prompt, tools)
            result["model_used"] = f"{model} -> {fb}"
            return result

    async def _tool_loop(
        self,
        model: str,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
    ) -> dict:
        url, auth_header = self._openai_endpoint(model)
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        tool_schemas = [openai_tool_schema(t) for t in tools]
        tool_index = {t.name: t for t in tools}
        total_in = 0
        total_out = 0
        tool_call_log: list[dict] = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for iteration in range(MAX_TOOL_ITERATIONS):
                resp = await client.post(
                    url,
                    headers=auth_header,
                    json={
                        "model": self._strip_openrouter_prefix(model),
                        "messages": messages,
                        "tools": tool_schemas,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                total_in += usage.get("prompt_tokens", 0)
                total_out += usage.get("completion_tokens", 0)

                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    return {
                        "content": msg.get("content", "") or "",
                        "tokens_in": total_in,
                        "tokens_out": total_out,
                        "model_used": model,
                        "tool_calls": tool_call_log,
                        "iterations": iteration + 1,
                    }

                messages.append(msg)
                for call in tool_calls:
                    call_id = call.get("id") or ""
                    fn = call.get("function") or {}
                    name = fn.get("name") or ""
                    args_json = fn.get("arguments") or "{}"
                    spec = tool_index.get(name)
                    if spec is None:
                        result_str = json.dumps({"error": f"unknown tool: {name}"})
                    else:
                        result_str = await execute_tool(spec, args_json)
                    tool_call_log.append({
                        "iteration": iteration,
                        "name": name,
                        "arguments": args_json,
                        "result": result_str,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_str,
                    })

        logger.warning("tool-use hit MAX_TOOL_ITERATIONS (%d); returning partial", MAX_TOOL_ITERATIONS)
        return {
            "content": "[tool-use loop exceeded max iterations; partial progress in tool_calls]",
            "tokens_in": total_in,
            "tokens_out": total_out,
            "model_used": model,
            "tool_calls": tool_call_log,
            "iterations": MAX_TOOL_ITERATIONS,
            "truncated": True,
        }

    def _openai_endpoint(self, model: str) -> tuple[str, dict]:
        """Pick the OpenAI-compatible endpoint + auth header for a given model."""
        if model.startswith("openrouter/"):
            return (
                "https://openrouter.ai/api/v1/chat/completions",
                {"Authorization": f"Bearer {self.openrouter_key}"},
            )
        return (f"{self.ollama_url}/chat/completions", {})

    @staticmethod
    def _strip_openrouter_prefix(model: str) -> str:
        return model[len("openrouter/"):] if model.startswith("openrouter/") else model
