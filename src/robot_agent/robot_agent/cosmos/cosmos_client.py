from typing import List, Optional

import httpx


class CosmosClient:
    """Async HTTP client to the Cosmos Reason2 vLLM server (OpenAI-compatible API).

    Creates a fresh connection per request to avoid event-loop binding issues
    when the client is instantiated in a non-async context (e.g. ROS2 node __init__)
    and later used from the uvicorn event loop.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        model: str = "nvidia/Cosmos-Reason2-8B",
    ):
        self._base_url = base_url.rstrip('/')
        self._model = model

    async def chat_completion(
        self,
        messages: List[dict],
        temperature: float = 0.6,
        max_tokens: int = 3000,
    ) -> str:
        """Call /v1/chat/completions on the vLLM server."""
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str):
        self._model = value

    @property
    def base_url(self) -> str:
        return self._base_url

    async def get_served_model(self) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("data"):
                        return data["data"][0]["id"]
        except Exception:
            pass
        return None

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/v1/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def close(self):
        pass  # no persistent client to close
