"""
Copyright 2026, Vijay Challa - Use of this source code follows the MIT license found in the LICENSE file.

Huntress Plugin - BisectHosting controls for the Huntress Minecraft server.

The Starbase client API is documented at
https://games.bisecthosting.com/docs#tag/Discord-Integration
(OpenAPI: https://games.bisecthosting.com/api-docs/openapi.json).

The Discord Integration tag on that spec is panel OAuth account linking.
Connect and callback return 403 for API-key requests, so this plugin uses

the server console endpoints through plugins.bisect:

    GET  /api/client/servers/{server}
    GET  /api/client/servers/{server}/resources
    POST /api/client/servers/{server}/power

Commands:
    !mc-health  - Power state and resource usage
    !mc-start   - Send a start signal
    !mc-restart - Send a restart signal
    !mc-kill    - Send a kill signal
"""

import asyncio
import logging
from typing import Optional

import aiohttp
from disnake import Embed

from artemis.permissions.resolver import Permission
from artemis.plugin.base import PluginHelper, PluginInterface
from artemis.events.listener import EventListener
from artemis.utils.helpers import format_bytes

from plugins.bisect.bisect import BisectApiError, BisectClient


logger = logging.getLogger("artemis.plugin.huntress")

STATE_COLORS = {
    "running": 0x3BA55D,
    "starting": 0xFAA61A,
    "stopping": 0xFAA61A,
    "offline": 0xED4245,
}


class Huntress(PluginInterface, PluginHelper):
    """Huntress plugin for the BisectHosting Minecraft server."""

    @staticmethod
    def register(bot):
        """Register the plugin."""
        if Huntress.is_testing_client(bot):
            bot.log.info("Not adding huntress commands on testing.")
            return

        bot.eventManager.add_listener(
            EventListener.new()
            .add_command("mc-health")
            .set_callback(Huntress.health)
            .set_help(
                "**Usage**: `!mc-health`\n\n"
                "Shows the Huntress Minecraft server's power state, resource usage, "
                "and Minecraft stats from BisectHosting. Resource numbers are cached "
                "by the panel for up to 20 seconds."
            )
        )

        bot.eventManager.add_listener(
            EventListener.new()
            .add_command("mc-start")
            .set_callback(Huntress.start)
            .set_help(
                "**Usage**: `!mc-start`\n\n"
                "Sends a start signal to the Huntress Minecraft server. "
                "Requires permission `p.huntress.start` (admins by default)."
            )
        )

        bot.eventManager.add_listener(
            EventListener.new()
            .add_command("mc-restart")
            .set_callback(Huntress.restart)
            .set_help(
                "**Usage**: `!mc-restart`\n\n"
                "Sends a restart signal to the Huntress Minecraft server. "
                "Requires permission `p.huntress.restart` (admins by default)."
            )
        )

        bot.eventManager.add_listener(
            EventListener.new()
            .add_command("mc-kill")
            .set_callback(Huntress.kill)
            .set_help(
                "**Usage**: `!mc-kill`\n\n"
                "Sends a kill signal to the Huntress Minecraft server. This stops "
                "the process immediately, without a graceful shutdown. "
                "Requires permission `p.huntress.kill` (admins by default)."
            )
        )

    @staticmethod
    async def health(data):
        """Handle !mc-health."""
        try:
            if not await Huntress._allowed(data, "p.huntress.health", True):
                return

            client = Huntress._client(data)
            if client is None:
                await Huntress._missing_config(data)
                return

            server, stats = await _gather_status(client)
            embed = Huntress.build_health_embed(server, stats)
            await data.message.reply(embed=embed)
        except BisectApiError as error:
            await Huntress._api_error(data, error)
        except aiohttp.ClientError as error:
            await data.message.reply(f"Could not reach BisectHosting: {error}")
        except Exception as error:
            await Huntress.exception_handler(data.message, error)

    @staticmethod
    async def start(data):
        """Handle !mc-start."""
        await Huntress._power(data, "start", "p.huntress.start")

    @staticmethod
    async def restart(data):
        """Handle !mc-restart."""
        await Huntress._power(data, "restart", "p.huntress.restart")

    @staticmethod
    async def kill(data):
        """Handle !mc-kill."""
        await Huntress._power(data, "kill", "p.huntress.kill")

    @staticmethod
    async def _power(data, signal: str, permission: str):
        """Send a power signal and report the result."""
        try:
            if not await Huntress._allowed(data, permission, False):
                return

            client = Huntress._client(data)
            if client is None:
                await Huntress._missing_config(data)
                return

            await client.send_power(signal)
            name = await Huntress._server_name(client)
            author = data.message.author
            logger.info(
                "Sent %s signal for Huntress server (user %s)",
                signal,
                author.id,
            )

            if signal == "kill":
                action = "Kill signal sent"
                detail = "The server process was stopped immediately."
            elif signal == "start":
                action = "Start signal sent"
                detail = "The server is starting."
            else:
                action = "Restart signal sent"
                detail = "The server is restarting."

            target = f"**{name}**" if name else "the Huntress server"
            await data.message.reply(f"{action} to {target}. {detail}")
        except BisectApiError as error:
            await Huntress._api_error(data, error)
        except aiohttp.ClientError as error:
            await data.message.reply(f"Could not reach BisectHosting: {error}")
        except Exception as error:
            await Huntress.exception_handler(data.message, error)

    @staticmethod
    def build_health_embed(server_payload: Optional[dict], stats_payload: Optional[dict]) -> Embed:
        """Build the health status embed from server and resource payloads."""
        server = _attributes(server_payload)
        stats = _attributes(stats_payload)
        resources = stats.get("resources") or {}
        limits = server.get("limits") or {}
        minecraft = stats.get("minecraft_resources") or {}

        state_key = str(stats.get("current_state") or "unknown").lower()
        name = server.get("name") or "Huntress"
        color = 0xE67E22 if stats.get("is_suspended") else STATE_COLORS.get(state_key, 0x5865F2)

        embed = Embed(title=name, color=color)
        nest = server.get("nest_name")
        if nest:
            embed.description = str(nest)

        embed.add_field(name="State", value=_state_label(server, stats), inline=True)

        node = server.get("node")
        if node:
            embed.add_field(name="Node", value=str(node), inline=True)

        address = _default_address(server)
        if address:
            embed.add_field(name="Address", value=address, inline=True)

        embed.add_field(name="CPU", value=_cpu_label(resources, limits), inline=True)
        embed.add_field(name="Memory", value=_memory_label(resources, limits), inline=True)
        embed.add_field(name="Disk", value=_disk_label(resources, limits), inline=True)
        embed.add_field(
            name="Network",
            value=(
                f"↓ {format_bytes(int(resources.get('network_rx_bytes') or 0))}\n"
                f"↑ {format_bytes(int(resources.get('network_tx_bytes') or 0))}"
            ),
            inline=True,
        )
        embed.add_field(name="Uptime", value=_format_uptime(resources.get("uptime")), inline=True)

        if minecraft:
            embed.add_field(name="TPS", value=_number(minecraft.get("minecraft_tps")), inline=True)
            embed.add_field(
                name="Entities",
                value=_number(minecraft.get("minecraft_entities"), decimals=0),
                inline=True,
            )
            embed.add_field(
                name="Chunks",
                value=_number(minecraft.get("minecraft_chunks"), decimals=0),
                inline=True,
            )
            minecraft_memory = minecraft.get("minecraft_memory_bytes")
            if minecraft_memory:
                embed.add_field(
                    name="Minecraft memory",
                    value=format_bytes(int(minecraft_memory)),
                    inline=True,
                )

        identifier = server.get("identifier")
        footer = "BisectHosting · resource stats cached up to 20 seconds"
        if identifier:
            footer = f"{identifier} · {footer}"
        embed.set_footer(text=footer)
        return embed

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
    async def _allowed(data, permission: str, default: bool) -> bool:
        check = Permission(permission, data.artemis, default)
        check.add_message_context(data.message)
        if await check.resolve():
            return True
        await check.send_unauthorized_message(data.message.channel)
        return False

    @staticmethod
    async def _missing_config(data):
        await data.message.reply(
            "Huntress is not configured. Set `HUNTRESS_BISECT_API_KEY` and "
            "`HUNTRESS_SERVER_ID` in `config/config.py`."
        )

    @staticmethod
    async def _api_error(data, error: BisectApiError):
        logger.warning("BisectHosting API error %s: %s", error.status, error.detail)
        await data.message.reply(f"BisectHosting returned an error ({error.status}): {error.detail}")

    @staticmethod
    async def _server_name(client: BisectClient) -> Optional[str]:
        try:
            payload = await client.get_server()
        except (BisectApiError, aiohttp.ClientError):
            logger.warning("Power signal succeeded but server name lookup failed", exc_info=True)
            return None
        name = _attributes(payload).get("name")
        return str(name) if name else None


async def _gather_status(client: BisectClient):
    """Fetch server details and resource stats."""
    return await asyncio.gather(client.get_server(), client.get_resources())


def _attributes(payload: Optional[dict]) -> dict:
    if not isinstance(payload, dict):
        return {}
    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        return attributes
    return payload


def _state_label(server: dict, stats: dict) -> str:
    state = str(stats.get("current_state") or "unknown").replace("_", " ").title()
    flags = []
    if stats.get("is_suspended") or server.get("is_suspended"):
        flags.append("suspended")
    if stats.get("is_sleeping"):
        flags.append("sleeping")
    if server.get("is_installing"):
        flags.append("installing")
    if server.get("is_transferring"):
        flags.append("transferring")
    if flags:
        return f"{state} ({', '.join(flags)})"
    return state


def _default_address(server: dict) -> Optional[str]:
    relationships = server.get("relationships") or {}
    allocations = relationships.get("allocations") or {}
    entries = allocations.get("data") if isinstance(allocations, dict) else None
    if not entries:
        return None

    chosen = None
    for entry in entries:
        attributes = (entry or {}).get("attributes") or {}
        if attributes.get("is_default"):
            chosen = attributes
            break
    if chosen is None:
        chosen = (entries[0] or {}).get("attributes") or {}

    host = chosen.get("ip_alias") or chosen.get("ip")
    if not host or str(host).strip().lower() == "shared":
        host = chosen.get("ip")
    port = chosen.get("port")
    if host and port:
        return f"{host}:{port}"
    if host:
        return str(host)
    return None


def _cpu_label(resources: dict, limits: dict) -> str:
    used = float(resources.get("cpu_absolute") or 0)
    limit = limits.get("cpu") or 0
    if limit:
        return f"{used:.1f}% / {limit}%"
    return f"{used:.1f}%"


def _memory_label(resources: dict, limits: dict) -> str:
    used = format_bytes(int(resources.get("memory_bytes") or 0))
    limit = limits.get("memory")
    if limit:
        return f"{used} / {limit} MiB"
    return used


def _disk_label(resources: dict, limits: dict) -> str:
    used = format_bytes(int(resources.get("disk_bytes") or 0))
    limit = limits.get("disk")
    if limit:
        return f"{used} / {limit} MiB"
    return used


def _format_uptime(uptime) -> str:
    if uptime is None:
        return "unknown"
    total_seconds = max(int(uptime), 0) // 1000
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _number(value, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if decimals == 0:
        return str(int(number))
    return f"{number:.{decimals}f}"
