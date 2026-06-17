"""LM Studio client using the OpenAI-compatible REST API."""

import json
import requests
from typing import Any, Dict

import config




class LMStudioClient:
    """Thin wrapper around LM Studio's /v1/chat/completions endpoint."""

    def __init__(self):
        self.base_url = config.LM_STUDIO_BASE_URL
        self.model = config.LM_STUDIO_MODEL

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        """Send a prompt and return the response parsed as a JSON dict.

        Strips markdown code fences if the model wraps its output in them.
        Raises requests.HTTPError on bad status, json.JSONDecodeError on bad output.
        """
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

        content = response.json()["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences: ```json ... ``` or ``` ... ```
        if content.startswith("```"):
            lines = content.split("\n")
            # Drop first line (```json or ```) and last line (```)
            inner = lines[1:] if lines[-1].strip() != "```" else lines[1:-1]
            content = "\n".join(inner).strip()

        return json.loads(content)

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
