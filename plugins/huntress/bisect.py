"""
Copyright 2026, Vijay Challa - Use of this source code follows the MIT license found in the LICENSE file.

Starbase client API used by the Huntress plugin.

Docs: https://games.bisecthosting.com/api-docs/openapi.json
"""

import json
from typing import Any, Optional
from urllib.parse import quote

import aiohttp


class BisectApiError(Exception):
    """Error returned by the Starbase client API."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class BisectClient:
    """
    Small client for the BisectHosting Starbase client API.

    Authenticated with an account API key sent as a bearer token.
    """

    def __init__(self, base_url: str, api_key: str, server_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.server_id = quote(server_id, safe="-")

    async def get_server(self) -> dict:
        """Server details, limits, and allocations."""
        return await self._request("GET", f"/api/client/servers/{self.server_id}")

    async def get_resources(self) -> dict:
        """Current power state and resource usage. Cached by the panel for 20 seconds."""
        return await self._request("GET", f"/api/client/servers/{self.server_id}/resources")

    async def send_power(self, signal: str) -> Optional[dict]:
        """
        Send a power signal.

        Accepted signals include start, stop, restart, kill, wake, sleep,
        safe-stop, and safe-restart. Success is an empty 204.
        """
        return await self._request(
            "POST",
            f"/api/client/servers/{self.server_id}/power",
            {"signal": signal},
        )

    async def _request(self, method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=20)
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, f"{self.base_url}{path}", **kwargs) as response:
                text = await response.text()
                payload = _decode_json(text)
                if response.status >= 400:
                    raise BisectApiError(response.status, _error_detail(payload, response.status))
                return payload


def _decode_json(text: str) -> Any:
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _error_detail(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            details = []
            for error in errors:
                if isinstance(error, dict) and error.get("detail"):
                    details.append(str(error["detail"]))
            if details:
                return "; ".join(details)
        if payload.get("detail"):
            return str(payload["detail"])
    return f"Request failed with status {status}."
