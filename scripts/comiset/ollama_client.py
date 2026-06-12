from __future__ import annotations

from typing import Any


class OllamaGateway:
    def __init__(self, host: str, pull_missing: bool = True, timeout_seconds: int | None = None) -> None:
        try:
            import ollama
        except ImportError as exc:
            raise SystemExit(
                "Missing dependency 'ollama'. Run 'uv sync' or install dependencies from pyproject.toml."
            ) from exc

        self._client = ollama.Client(host=host, timeout=timeout_seconds)
        self._pull_missing = pull_missing
        self._ready: set[str] = set()

    def ensure_model(self, model: str) -> None:
        if model in self._ready:
            return
        try:
            self._client.show(model)
        except Exception:
            if not self._pull_missing:
                raise
            print(f"Model {model!r} not found locally. Pulling with Ollama...")
            self._client.pull(model)
            self._client.show(model)
        self._ready.add(model)

    def chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int | None = None,
    ) -> str:
        self.ensure_model(model)
        options: dict[str, Any] = {"temperature": 0}
        response = self._client.chat(
            model=model,
            stream=False,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options=options,
        )
        if hasattr(response, "message"):
            message = response.message
            if hasattr(message, "content"):
                return message.content
        if isinstance(response, dict):
            message = response.get("message", {})
            if isinstance(message, dict):
                return str(message.get("content", ""))
        raise RuntimeError(f"Unexpected Ollama response shape: {type(response)!r}")
