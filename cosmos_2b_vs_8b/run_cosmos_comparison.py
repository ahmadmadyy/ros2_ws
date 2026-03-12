#!/usr/bin/env python3
"""
Send the screw trace analysis prompt to Cosmos-Reason2-2B and Cosmos-Reason2-8B
and save each response in this directory.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx

VLLM_URL = "http://localhost:8000"
PROMPT_FILE = Path(__file__).parent / "prompt_screw_trace_analysis.txt"
OUT_DIR = Path(__file__).parent

MODELS = [
    "nvidia/Cosmos-Reason2-2B",
    "nvidia/Cosmos-Reason2-8B",
]


async def call_model(client: httpx.AsyncClient, model: str, prompt: str) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_tokens": 4096,
    }
    print(f"  Sending to {model} ...")
    resp = await client.post(f"{VLLM_URL}/v1/chat/completions", json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def model_slug(model: str) -> str:
    # e.g. "nvidia/Cosmos-Reason2-2B" -> "cosmos_reason2_2b"
    name = model.split("/")[-1]          # Cosmos-Reason2-2B
    return name.lower().replace("-", "_")


async def main():
    prompt = PROMPT_FILE.read_text()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    async with httpx.AsyncClient(timeout=600.0) as client:
        # quick availability check
        try:
            r = await client.get(f"{VLLM_URL}/v1/models", timeout=5.0)
            r.raise_for_status()
            served = [m["id"] for m in r.json().get("data", [])]
            print(f"vLLM server online. Served models: {served}\n")
        except Exception as e:
            print(f"ERROR: vLLM server not reachable at {VLLM_URL} — {e}")
            return

        for model in MODELS:
            slug = model_slug(model)
            out_path = OUT_DIR / f"response_{slug}_{ts}.txt"
            meta_path = OUT_DIR / f"response_{slug}_{ts}.json"

            try:
                t0 = asyncio.get_event_loop().time()
                response = await call_model(client, model, prompt)
                elapsed = asyncio.get_event_loop().time() - t0

                # Save raw text
                out_path.write_text(response)

                # Save metadata + response together
                meta = {
                    "model": model,
                    "timestamp": ts,
                    "elapsed_sec": round(elapsed, 2),
                    "prompt_file": str(PROMPT_FILE),
                    "vllm_url": VLLM_URL,
                    "response": response,
                }
                meta_path.write_text(json.dumps(meta, indent=2))

                print(f"  [{model}] Done in {elapsed:.1f}s")
                print(f"    text  → {out_path.name}")
                print(f"    json  → {meta_path.name}\n")

            except Exception as e:
                print(f"  ERROR calling {model}: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
