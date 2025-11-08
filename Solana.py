import discord
import aiohttp
import asyncio
import os

# Environment variables
TOKEN = os.environ.get('TOKEN')
GUILD_ID = os.environ.get('GUILD_ID')  # optional
COIN = "solana"  # ✅ Solana (SOL) on CoinGecko

if not TOKEN:
    raise SystemExit("Missing env var TOKEN")
if GUILD_ID:
    try:
        GUILD_ID = int(GUILD_ID)
    except ValueError:
        raise SystemExit("GUILD_ID must be an integer")

# Discord intents
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # also enable "Server Members Intent" in Dev Portal
client = discord.Client(intents=intents)

update_task = None

# --- Price fetcher ---
async def get_price_data():
    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={COIN}"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"CoinGecko HTTP {resp.status}")
            data = await resp.json()
            price = data[0]['current_price']
            change_24h = data[0]['price_change_percentage_24h']
            return price, change_24h

# --- Per-guild update ---
async def update_guild(guild: discord.Guild):
    try:
        me = guild.me or await guild.fetch_member(client.user.id)
    except discord.HTTPException as e:
        print(f"[{guild.name}] Could not fetch bot member: {e}")
        return

    perms = me.guild_permissions
    if not perms.change_nickname and not perms.manage_nicknames:
        print(f"[{guild.name}] Missing permission: Change Nickname or Manage Nicknames.")
        return

    try:
        price, change_24h = await get_price_data()
    except Exception as e:
        print(f"[{guild.name}] Price fetch failed: {e}")
        return

    emoji = "🟢" if change_24h >= 0 else "🔴"

    # Nickname shows only price + emoji
    nickname = f"${price:.2f} {emoji}"
    if len(nickname) > 32:
        nickname = nickname[:32]

    try:
        await me.edit(nick=nickname, reason="Auto price update")
        await client.change_presence(activity=discord.Game(name=f"24h change {change_24h:+.2f}%"))
        print(f"[{guild.name}] Nick → {nickname}, 24h change {change_24h:+.2f}%")
    except discord.Forbidden:
        print(f"[{guild.name}] Forbidden: role hierarchy or permissions issue.")
    except discord.HTTPException as e:
        print(f"[{guild.name}] HTTP error updating nickname: {e}")

# --- Main loop ---
async def updater_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            target_guilds = []
            if GUILD_ID:
                g = client.get_guild(GUILD_ID)
                if g:
                    target_guilds = [g]
                else:
                    print("Configured GUILD_ID not found.")
            else:
                target_guilds = list(client.guilds)

            if not target_guilds:
                print("No guilds found.")
            else:
                await asyncio.gather(*(update_guild(g) for g in target_guilds))
        except Exception as e:
            print(f"Updater loop error: {e}")

        await asyncio.sleep(60)  # update interval

# --- Startup ---
@client.event
async def on_ready():
    global update_task
    print(f"Logged in as {client.user} in {len(client.guilds)} guild(s).")
    if update_task is None or update_task.done():
        update_task = asyncio.create_task(updater_loop())

if __name__ == "__main__":
    client.run(TOKEN)
