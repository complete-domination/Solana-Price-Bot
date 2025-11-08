import os
import asyncio
import logging
from typing import Tuple, Optional

import aiohttp
import discord

# ---------- Config ----------
TOKEN = os.environ.get("TOKEN")
GUILD_ID_RAW = os.environ.get("GUILD_ID")  # optional; if unset, update all guilds
COIN = "solana"  # SOL on CoinGecko
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))

if not TOKEN:
    raise SystemExit("Missing env var TOKEN")

GUILD_ID: Optional[int] = None
if GUILD_ID_RAW:
    try:
        GUILD_ID = int(GUILD_ID_RAW)
    except ValueError:
        raise SystemExit("GUILD_ID must be an integer if provided")

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("icp-bot")

# ---------- Discord client ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # enable Server Members Intent in Dev Portal
client = discord.Client(intents=intents)

# Global session + task
_http_session: Optional[aiohttp.ClientSession] = None
update_task: Optional[asyncio.Task] = None


# ---------- HTTP helpers ----------
async def get_price_data(session: aiohttp.ClientSession) -> Tuple[float, float]:
    """
    Fetch price and 24h change from CoinGecko with retries.
    Returns: (price_usd, change_24h_percent)
    Raises on persistent failure.
    """
    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={COIN}"
    backoffs = [0, 1.5, 3.0, 5.0]  # seconds

    for attempt, delay in enumerate(backoffs):
        if delay:
            await asyncio.sleep(delay)
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if not isinstance(data, list) or not data:
                        raise RuntimeError("CoinGecko returned empty or invalid list")
                    row = data[0]
                    price = float(row["current_price"])
                    change = float(row["price_change_percentage_24h"])
                    return price, change
                elif resp.status in (429, 500, 502, 503, 504):
                    log.warning(f"CoinGecko {resp.status} (attempt {attempt+1})")
                else:
                    text = await resp.text()
                    raise RuntimeError(f"CoinGecko HTTP {resp.status}: {text[:200]}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning(f"HTTP error (attempt {attempt+1}): {e}")

    raise RuntimeError("Failed to fetch ICP price after retries")


# ---------- Per-guild update ----------
async def update_guild(guild: discord.Guild):
    # Refresh member (don’t rely solely on cache)
    try:
        me = guild.me or await guild.fetch_member(client.user.id)
    except discord.HTTPException as e:
        log.warning(f"[{guild.name}] Could not fetch bot member: {e}")
        return

    perms = me.guild_permissions
    if not (perms.change_nickname or perms.manage_nicknames):
        log.info(f"[{guild.name}] Missing permission: Change Nickname (or Manage Nicknames).")
        return

    try:
        assert _http_session is not None, "HTTP session not initialized"
        price, change_24h = await get_price_data(_http_session)
    except Exception as e:
        log.warning(f"[{guild.name}] Price fetch failed: {e}")
        # Keep presence informative on failure
        try:
            await client.change_presence(activity=discord.Game(name="price: API error"))
        except Exception:
            pass
        return

    emoji = "🟢" if change_24h >= 0 else "🔴"
    nickname = f"${price:.2f} {emoji}"
    if len(nickname) > 32:
        nickname = nickname[:32]

    try:
        await me.edit(nick=nickname, reason="Auto price update")
    except discord.Forbidden:
        log.info(f"[{guild.name}] Forbidden: role hierarchy/permissions block nickname change.")
    except discord.HTTPException as e:
        log.warning(f"[{guild.name}] HTTP error updating nick: {e}")

    # Presence under the name
    try:
        await client.change_presence(activity=discord.Game(name=f"24h change {change_24h:+.2f}%"))
    except Exception as e:
        log.debug(f"[{guild.name}] Could not set presence: {e}")

    log.info(f"[{guild.name}] Nick → {nickname} | 24h → {change_24h:+.2f}%")


# ---------- Updater loop ----------
async def updater_loop():
    await client.wait_until_ready()
    log.info("Updater loop started.")
    while not client.is_closed():
        try:
            # Resolve target guilds
            if GUILD_ID:
                g = client.get_guild(GUILD_ID)
                targets = [g] if g else []
                if not g:
                    log.info("Configured GUILD_ID not found yet. Is the bot in that server?")
            else:
                targets = list(client.guilds)

            if not targets:
                log.info("No guilds to update yet.")
            else:
                await asyncio.gather(*(update_guild(g) for g in targets))
        except Exception as e:
            log.error(f"Updater loop error: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)


# ---------- Discord events ----------
@client.event
async def on_ready():
    global update_task, _http_session
    log.info(f"Logged in as {client.user} in {len(client.guilds)} guild(s).")

    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()

    if update_task is None or update_task.done():
        update_task = asyncio.create_task(updater_loop())


@client.event
async def on_disconnect():
    log.warning("Discord disconnected.")


@client.event
async def on_resumed():
    log.info("Discord session resumed.")


# Graceful shutdown for Railway restarts
async def _shutdown():
    global _http_session, update_task
    if update_task and not update_task.done():
        update_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await update_task
    if _http_session and not _http_session.closed:
        await _http_session.close()


# ---------- Entrypoint ----------
if __name__ == "__main__":
    try:
        client.run(TOKEN)
    except KeyboardInterrupt:
        pass
