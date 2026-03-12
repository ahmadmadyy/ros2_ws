from typing import Optional

import httpx


class OllamaClient:
    """Async HTTP client for a local Ollama server."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2",
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http = httpx.AsyncClient(timeout=360.0)

    async def chat(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.1,
        json_format: bool = True,
    ) -> str:
        """Send a single-turn chat to Ollama and return the reply text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_format:
            payload["format"] = "json"

        resp = await self._http.post(
            f"{self._base_url}/api/chat",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]

    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str):
        self._model = value

    @property
    def base_url(self) -> str:
        return self._base_url

    async def is_available(self) -> bool:
        """Return True if the Ollama server is reachable."""
        try:
            resp = await self._http.get(f"{self._base_url}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list:
        """Return list of locally available model names."""
        try:
            resp = await self._http.get(f"{self._base_url}/api/tags", timeout=5.0)
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            pass
        return []

    async def close(self):
        await self._http.aclose()
