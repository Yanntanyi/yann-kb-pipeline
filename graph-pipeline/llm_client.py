"""LLM clients for the graph pipeline.

Two interchangeable backends, both exposing the same interface
(`generate_json` / `generate_text`) so the rest of the pipeline never has to
know which one is in use:

  - LMStudioClient: local LM Studio server (OpenAI-compatible, no auth)
  - WatsonxClient:  IBM watsonx.ai text/chat endpoint (IAM bearer auth)

Use `get_llm_client()` to obtain the one selected by `config.LLM_PROVIDER`.
"""

import json
import threading
import time
from typing import Any, Dict, Optional

import requests

import config


def _extract_json_object(content: str) -> Dict[str, Any]:
    """Parse a JSON object out of an LLM response.

    Tolerant of markdown code fences and of reasoning models (e.g. gpt-oss)
    that wrap the JSON in explanatory prose. Falls back to grabbing the
    outermost {...} block. Raises json.JSONDecodeError if nothing parses.
    """
    content = content.strip()

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    if content.startswith("```"):
        lines = content.split("\n")
        inner = lines[1:] if lines[-1].strip() != "```" else lines[1:-1]
        content = "\n".join(inner).strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Reasoning models may emit prose around the JSON — take the
        # outermost object and try that.
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise


class LMStudioClient:
    """Thin wrapper around LM Studio's /v1/chat/completions endpoint."""

    def __init__(self):
        self.base_url = config.LM_STUDIO_BASE_URL
        self.model = config.LM_STUDIO_MODEL

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        """Send a prompt and return the response parsed as a JSON dict."""
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 2048,
            },
            timeout=120,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _extract_json_object(content)

    def generate_text(self, prompt: str, max_tokens: int = 1024) -> str:
        """Send a prompt and return the raw text response (no JSON parsing)."""
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    def embed(self, texts):
        """Return one embedding vector per input text via /v1/embeddings.

        Requires an embedding model loaded in LM Studio (config.LM_STUDIO_EMBED_MODEL).
        """
        if not texts:
            return []
        response = requests.post(
            f"{self.base_url}/embeddings",
            json={"model": config.LM_STUDIO_EMBED_MODEL, "input": texts},
            timeout=120,
        )
        response.raise_for_status()
        return [row["embedding"] for row in response.json()["data"]]


class WatsonxClient:
    """Client for IBM watsonx.ai's text/chat endpoint.

    watsonx.ai is NOT OpenAI-compatible despite serving models like
    `openai/gpt-oss-120b`: it requires an IAM bearer token (exchanged from an
    API key, ~1h lifetime) and a request body carrying `model_id` and
    `project_id`. This class hides both behind the same `generate_json` /
    `generate_text` interface as LMStudioClient, so it's a drop-in swap.
    """

    IAM_URL = "https://iam.cloud.ibm.com/identity/token"

    def __init__(self):
        self.base_url = config.WATSONX_BASE_URL.rstrip("/")
        self.api_key = config.WATSONX_API_KEY
        self.project_id = config.WATSONX_PROJECT_ID
        self.model = config.WATSONX_MODEL
        self.api_version = config.WATSONX_API_VERSION

        if not self.api_key or not self.project_id:
            raise RuntimeError(
                "watsonx requires WATSONX_API_KEY and WATSONX_PROJECT_ID. "
                "Set them in config.py or as environment variables."
            )

        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._token_lock = threading.Lock()

    # ── IAM token management ──────────────────────────────────────────────

    def _get_token(self) -> str:
        """Return a valid IAM bearer token, refreshing it when near expiry.

        Tokens last ~3600s; we refresh 60s early. Thread-safe so the client
        can be shared if the pipeline ever parallelizes calls.
        """
        with self._token_lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token

            response = requests.post(
                self.IAM_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                    "apikey": self.api_key,
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            self._token = payload["access_token"]
            self._token_expiry = time.time() + payload.get("expires_in", 3600)
            return self._token

    # ── Core request ──────────────────────────────────────────────────────

    def _chat(self, prompt: str, max_tokens: int, temperature: float = 0.1) -> str:
        """POST one user-turn chat completion and return the message content.

        Retries once on a 401 in case the token expired mid-flight.
        """
        url = f"{self.base_url}/ml/v1/text/chat?version={self.api_version}"
        body = {
            "model_id": self.model,
            "project_id": self.project_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._get_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            response = requests.post(url, headers=headers, json=body, timeout=300)

            if response.status_code == 401 and attempt == 0:
                # Force a token refresh and retry once.
                with self._token_lock:
                    self._token = None
                continue

            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()

        # Unreachable: the loop either returns or raises.
        raise RuntimeError("watsonx chat request failed after retry")

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        """Send a prompt and return the response parsed as a JSON dict."""
        content = self._chat(prompt, max_tokens=2048)
        return _extract_json_object(content)

    def generate_text(self, prompt: str, max_tokens: int = 1024) -> str:
        """Send a prompt and return the raw text response (no JSON parsing)."""
        return self._chat(prompt, max_tokens=max_tokens)

    def embed(self, texts):
        """Return one embedding vector per input text via /ml/v1/text/embeddings.

        Batches inputs (config.WATSONX_EMBED_BATCH) to stay within watsonx's
        per-request input limit. Uses config.WATSONX_EMBED_MODEL.
        """
        if not texts:
            return []

        url = f"{self.base_url}/ml/v1/text/embeddings?version={self.api_version}"
        vectors = []

        for start in range(0, len(texts), config.WATSONX_EMBED_BATCH):
            batch = texts[start : start + config.WATSONX_EMBED_BATCH]
            body = {
                "model_id": config.WATSONX_EMBED_MODEL,
                "project_id": self.project_id,
                "inputs": batch,
            }
            for attempt in range(2):
                headers = {
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                response = requests.post(url, headers=headers, json=body, timeout=120)
                if response.status_code == 401 and attempt == 0:
                    with self._token_lock:
                        self._token = None
                    continue
                response.raise_for_status()
                vectors.extend(
                    row["embedding"] for row in response.json()["results"]
                )
                break

        return vectors


def get_llm_client(provider: Optional[str] = None):
    """Return the LLM client selected by `config.LLM_PROVIDER` (or `provider`).

    Pass `provider` explicitly to override the config default — e.g. to run the
    graph-build phases on a local model while `ask.py` uses watsonx.
    """
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "watsonx":
        return WatsonxClient()
    if provider in ("lmstudio", "lm_studio", "local"):
        return LMStudioClient()
    raise ValueError(
        f"Unknown LLM provider: {provider!r} (expected 'watsonx' or 'lmstudio')"
    )
