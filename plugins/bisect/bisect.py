"""
Copyright 2026, Vijay Challa - Use of this source code follows the MIT license found in the LICENSE file.

Bisect plugin - Starbase client API.

Docs: https://games.bisecthosting.com/api-docs/openapi.json

Huntress calls BisectClient for health, restart, and kill. This plugin
registers the start command:

    !mc-start - Send a start signal
"""

import json
import logging
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from artemis.events.listener import EventListener
from artemis.permissions.resolver import Permission
from artemis.plugin.base import PluginHelper, PluginInterface

logger = logging.getLogger("artemis.plugin.bisect")


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


class Bisect(PluginInterface, PluginHelper):
    """Starbase client plugin. Registers the server start command."""

    @staticmethod
    def register(bot):
        """Register the plugin."""
        if Bisect.is_testing_client(bot):
            bot.log.info("Not adding bisect commands on testing.")
            return

        bot.eventManager.add_listener(
            EventListener.new()
            .add_command("mc-start")
            .set_callback(Bisect.start)
            .set_help(
                "**Usage**: `!mc-start`\n\n"
                "Sends a start signal to the Huntress Minecraft server. "
                "Requires permission `p.bisect.start` (admins by default)."
            )
        )

    @staticmethod
    async def start(data):
        """Handle !mc-start."""
        try:
            check = Permission("p.bisect.start", data.artemis, False)
            check.add_message_context(data.message)
            if not await check.resolve():
                await check.send_unauthorized_message(data.message.channel)
                return

            client = Bisect._client(data)
            if client is None:
                await data.message.reply(
                    "Bisect is not configured. Set `HUNTRESS_BISECT_API_KEY` and "
                    "`HUNTRESS_SERVER_ID` in `config/config.py`."
                )
                return

            await client.send_power("start")
            logger.info("Sent start signal for Huntress server (user %s)", data.message.author.id)
            name = await Bisect._server_name(client)
            target = f"**{name}**" if name else "the Huntress server"
            await data.message.reply(f"Start signal sent to {target}. The server is starting.")
        except BisectApiError as error:
            logger.warning("BisectHosting API error %s: %s", error.status, error.detail)
            await data.message.reply(f"BisectHosting returned an error ({error.status}): {error.detail}")
        except aiohttp.ClientError as error:
            await data.message.reply(f"Could not reach BisectHosting: {error}")
        except Exception as error:
            await Bisect.exception_handler(data.message, error)

    @staticmethod
    def _client(data) -> Optional[BisectClient]:
        config = data.artemis.config
        api_key = str(getattr(config, "HUNTRESS_BISECT_API_KEY", "") or "").strip()
        server_id = str(getattr(config, "HUNTRESS_SERVER_ID", "") or "").strip()
        base_url = str(
            getattr(config, "HUNTRESS_BISECT_API_URL", "https://games.bisecthosting.com")
            or "https://games.bisecthosting.com"
        ).strip()
        if not api_key or not server_id:
            return None
        return BisectClient(base_url, api_key, server_id)

    @staticmethod
    async def _server_name(client: BisectClient) -> Optional[str]:
        try:
            payload = await client.get_server()
        except (BisectApiError, aiohttp.ClientError):
            logger.warning("Start signal succeeded but server name lookup failed", exc_info=True)
            return None
        attributes = payload.get("attributes") if isinstance(payload, dict) else None
        if not isinstance(attributes, dict):
            attributes = payload if isinstance(payload, dict) else {}
        name = attributes.get("name")
        return str(name) if name else None


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
