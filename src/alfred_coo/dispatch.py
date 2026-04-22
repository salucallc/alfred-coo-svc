"""
Model dispatcher with multi-provider fallback support.
"""

import httpx
from anthropic import AsyncAnthropic

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
