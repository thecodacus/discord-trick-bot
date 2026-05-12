"""
Trick-the-AI Discord bot.

Each level has a forbidden secret word the bot is system-prompted to protect.
Users in a designated channel try to trick the bot into revealing it. If the
bot's reply contains the secret (case-insensitive substring match), the user
who tricked it gets the level's role and a celebration message.

Usage in chat:
    /level <n>          -- pick which level to attempt (defaults to highest
                           unsolved by you, or 1)
    @bot <message>      -- send a jailbreak attempt; bot replies in thread
    plain message       -- if posted in the dedicated channel, also treated
                           as an attempt (no @ needed)

Stateless: each message is a fresh single-turn conversation. No memory across
attempts, intentionally -- the game is one prompt at a time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("trick-bot")


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Level:
    n: int
    secret: str          # forbidden word (case-insensitive match)
    role_id: int         # discord role to grant on win
    system_prompt: str   # text loaded from prompts file, with {SECRET} substituted


def load_levels() -> list[Level]:
    levels: list[Level] = []
    n = 1
    while True:
        secret = os.getenv(f"LEVEL_{n}_SECRET")
        role_id = os.getenv(f"LEVEL_{n}_ROLE_ID")
        prompt_file = os.getenv(f"LEVEL_{n}_SYSTEM_PROMPT_FILE")
        if not (secret and role_id and prompt_file):
            break
        prompt_text = Path(prompt_file).read_text(encoding="utf-8")
        prompt_text = prompt_text.replace("{SECRET}", secret)
        levels.append(Level(n=n, secret=secret, role_id=int(role_id), system_prompt=prompt_text))
        n += 1
    if not levels:
        raise RuntimeError("No levels configured. See .env.example.")
    log.info("Loaded %d levels", len(levels))
    return levels


DISCORD_TOKEN     = os.environ["DISCORD_TOKEN"]
DISCORD_GUILD_ID  = int(os.environ["DISCORD_GUILD_ID"])
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
LLAMA_BASE_URL    = os.environ.get("LLAMA_BASE_URL", "http://localhost:8080").rstrip("/")
LLAMA_MODEL       = os.environ.get("LLAMA_MODEL", "llama")
LEVELS            = load_levels()
MAX_USER_INPUT    = 1500   # discord soft cap; truncate user input above this
MAX_BOT_REPLY     = 1900   # discord hard cap is 2000; leave room for trim suffix


# -----------------------------------------------------------------------------
# llama-server client
# -----------------------------------------------------------------------------

class LlamaClient:
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url
        self.model = model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def chat(self, system_prompt: str, user_message: str) -> str:
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            "temperature": 0.7,
            "max_tokens": 400,
            "stream": False,
        }
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            log.exception("llama-server request failed")
            return f"(the gatekeeper appears to be napping... [{e}])"
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            log.error("Unexpected response shape: %r", data)
            return "(the gatekeeper mumbled something incomprehensible)"

    async def aclose(self) -> None:
        await self._client.aclose()


llama = LlamaClient(LLAMA_BASE_URL, LLAMA_MODEL)


# -----------------------------------------------------------------------------
# Game logic
# -----------------------------------------------------------------------------

def detect_secret(text: str, secret: str) -> bool:
    """Case-insensitive substring match on the secret. Avoid matching when the
    secret is itself called out as a quoted refusal ('I cannot say BANANA')."""
    if not text or not secret:
        return False
    # Strip surrounding markdown / punctuation noise that might break the check.
    haystack = text.lower()
    needle = secret.lower()
    return needle in haystack


def pick_default_level(member: discord.Member, levels: list[Level]) -> Level:
    """Highest unsolved level (no role) for this user, else level 1."""
    member_role_ids = {r.id for r in member.roles}
    for lvl in levels:
        if lvl.role_id not in member_role_ids:
            return lvl
    # User has every role -- send them to the last level for replay flavor.
    return levels[-1]


def trim_reply(text: str) -> str:
    if len(text) <= MAX_BOT_REPLY:
        return text
    return text[:MAX_BOT_REPLY - 1] + "…"


# -----------------------------------------------------------------------------
# Discord client
# -----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# In-memory per-user current-level selection. Lost on bot restart -- fine.
USER_LEVEL: dict[int, int] = {}


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", client.user, client.user.id if client.user else "?")
    guild = discord.Object(id=DISCORD_GUILD_ID)
    await tree.sync(guild=guild)
    log.info("Slash commands synced to guild %d", DISCORD_GUILD_ID)


@tree.command(name="level", description="Pick a level to attempt.",
              guild=discord.Object(id=DISCORD_GUILD_ID))
@app_commands.describe(n="Level number (1-based).")
async def cmd_level(interaction: discord.Interaction, n: int) -> None:
    if not (1 <= n <= len(LEVELS)):
        await interaction.response.send_message(
            f"Levels are 1..{len(LEVELS)}.", ephemeral=True,
        )
        return
    USER_LEVEL[interaction.user.id] = n
    await interaction.response.send_message(
        f"You're now attempting **Level {n}**. Send a message in this channel.",
        ephemeral=True,
    )


@tree.command(name="status", description="See which levels you've cleared.",
              guild=discord.Object(id=DISCORD_GUILD_ID))
async def cmd_status(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Run this in the guild.", ephemeral=True)
        return
    role_ids = {r.id for r in interaction.user.roles}
    lines = []
    for lvl in LEVELS:
        marker = "✅" if lvl.role_id in role_ids else "⬜"
        lines.append(f"{marker} Level {lvl.n}")
    cur = USER_LEVEL.get(interaction.user.id) or pick_default_level(interaction.user, LEVELS).n
    lines.append(f"\nCurrent: **Level {cur}** (use `/level <n>` to switch).")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def handle_attempt(message: discord.Message) -> None:
    if message.author.bot:
        return
    if message.channel.id != DISCORD_CHANNEL_ID:
        return
    if not isinstance(message.author, discord.Member):
        return

    text = message.content.strip()
    # Strip bot mention prefix if present.
    if client.user:
        text = re.sub(rf"^<@!?{client.user.id}>\s*", "", text).strip()
    if not text:
        return

    if len(text) > MAX_USER_INPUT:
        text = text[:MAX_USER_INPUT]

    level_n = USER_LEVEL.get(message.author.id) or pick_default_level(message.author, LEVELS).n
    level = next(lvl for lvl in LEVELS if lvl.n == level_n)

    async with message.channel.typing():
        reply = await llama.chat(level.system_prompt, text)

    won = detect_secret(reply, level.secret)
    if won:
        guild = message.guild
        role = guild.get_role(level.role_id) if guild else None
        granted = False
        if role and role not in message.author.roles:
            try:
                await message.author.add_roles(role, reason=f"Cleared Level {level.n}")
                granted = True
            except discord.Forbidden:
                log.error("Missing Manage Roles perm or role above bot's role")
            except discord.HTTPException:
                log.exception("Failed to grant role")
        prefix = (
            f"🎉 **{message.author.mention} cracked Level {level.n}!** "
            f"{'Role granted.' if granted else 'Role grant failed (check bot perms).'}\n\n"
        )
        await message.reply(prefix + trim_reply(reply), mention_author=False)
    else:
        await message.reply(trim_reply(reply), mention_author=False)


@client.event
async def on_message(message: discord.Message) -> None:
    try:
        await handle_attempt(message)
    except Exception:
        log.exception("error handling message")


async def main() -> None:
    try:
        await client.start(DISCORD_TOKEN)
    finally:
        await llama.aclose()


if __name__ == "__main__":
    asyncio.run(main())
