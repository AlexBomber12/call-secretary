"""Bound decoded API bodies while streaming; never log response payloads."""

from __future__ import annotations

import json
from typing import Any

import httpx


async def json_body(response: httpx.Response, limit: int) -> Any:
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=8192):
        if len(body) + len(chunk) > limit:
            raise ValueError("API response exceeds size limit")
        body.extend(chunk)
    return json.loads(body)
