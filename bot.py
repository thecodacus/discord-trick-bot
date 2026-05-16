"""
Trick-the-AI Discord bot.

Each level has a forbidden secret word the bot is system-prompted to protect.
Users in a designated channel try to trick the bot into revealing it. If the
bot's reply contains the secret (case-insensitive substring match), the user
who tricked it gets the level's role and a celebration message.

Usage in chat:
    /level <n>          -- pick which level to attempt (defaults to highest
                           unsolved by you, or 1)
    @bot <message>      -- send a jailbreak attempt; bot replies in thread.
                           The bot only responds when explicitly @mentioned.

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
            "max_tokens": 4000,
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
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            log.error("Unexpected response shape: %r", data)
            return "(the gatekeeper mumbled something incomprehensible)"

        # Reasoning models (Gemma 4, Qwen3.x) may return their actual reply
        # inside <think>...</think> blocks, or place it in `reasoning_content`
        # while `content` is empty. Pull whichever has substance, strip the
        # thinking tags, and fall back gracefully if nothing usable remains.
        content   = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()

        # Strip <think>...</think> blocks (including nested/multiline) from content.
        content_clean = re.sub(r"<think>.*?</think>", "", content,
                               flags=re.DOTALL | re.IGNORECASE).strip()

        # Pick the best non-empty payload.
        for candidate in (content_clean, content, reasoning):
            if candidate:
                return candidate

        log.warning("LLM returned empty content; raw msg=%r", msg)
        return "(the gatekeeper is silent... try again, or rephrase)"

    async def stream_chat(self, system_prompt: str, user_message: str):
        """Async-generator that yields ('content'|'reasoning', text) chunks
        from a streaming completion. Caller is responsible for accumulating
        and rendering. On HTTP error, yields a single ('error', msg) chunk."""
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            "temperature": 0.7,
            "max_tokens": 4000,
            "stream": True,
        }
        try:
            async with self._client.stream("POST", url, json=payload,
                                            timeout=httpx.Timeout(300.0, connect=10.0)) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = chunk["choices"][0].get("delta", {}) or {}
                    except (KeyError, IndexError):
                        continue
                    c = delta.get("content")
                    r = delta.get("reasoning_content")
                    if c:
                        yield ("content", c)
                    if r:
                        yield ("reasoning", r)
        except httpx.HTTPError as e:
            log.exception("llama-server stream failed")
            yield ("error", str(e))

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
    if not client.user or client.user not in message.mentions:
        return

    text = message.content.strip()
    text = re.sub(rf"<@!?{client.user.id}>", "", text).strip()
    if not text:
        return

    if len(text) > MAX_USER_INPUT:
        text = text[:MAX_USER_INPUT]

    level_n = USER_LEVEL.get(message.author.id) or pick_default_level(message.author, LEVELS).n
    level = next(lvl for lvl in LEVELS if lvl.n == level_n)

    log.info("attempt user=%s (id=%s) level=%d secret=%r prompt=%r",
             message.author, message.author.id, level.n, level.secret, text)

    # Send a placeholder reply we'll edit in place as tokens stream in.
    placeholder = await message.reply("💭 *the gatekeeper is thinking...*",
                                       mention_author=False)

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    last_edit = 0.0  # monotonic timestamp
    edit_interval = 1.2  # seconds; Discord caps at ~5 edits / 5s / channel
    visible_now = ""

    async for kind, chunk in llama.stream_chat(level.system_prompt, text):
        if kind == "error":
            await placeholder.edit(content=f"(the gatekeeper is napping... [{chunk}])")
            return
        if kind == "content":
            content_parts.append(chunk)
        elif kind == "reasoning":
            reasoning_parts.append(chunk)

        # Render the visible portion: strip closed <think> blocks. If we're
        # mid-think (open tag with no close yet), only show pre-think text.
        full = "".join(content_parts)
        closed_stripped = re.sub(r"<think>.*?</think>", "", full,
                                  flags=re.DOTALL | re.IGNORECASE)
        last_open = closed_stripped.rfind("<think>")
        if last_open != -1:
            visible = closed_stripped[:last_open].rstrip() + "\n*…thinking…*"
        else:
            visible = closed_stripped.strip()

        if not visible:
            visible = "💭 *the gatekeeper is thinking...*"

        # Throttle edits.
        now = asyncio.get_event_loop().time()
        if now - last_edit >= edit_interval and visible != visible_now:
            try:
                await placeholder.edit(content=trim_reply(visible))
                visible_now = visible
                last_edit = now
            except discord.HTTPException:
                # 429 or transient -- discord.py auto-handles, just skip.
                pass

    # Stream finished. Final reply = fully-stripped content.
    full = "".join(content_parts)
    final = re.sub(r"<think>.*?</think>", "", full,
                   flags=re.DOTALL | re.IGNORECASE).strip()
    if not final:
        final = "(the gatekeeper is silent... try again, or rephrase)"

    think_blocks = "\n---\n".join(re.findall(r"<think>(.*?)</think>", full,
                                              flags=re.DOTALL | re.IGNORECASE))
    reasoning_full = "".join(reasoning_parts).strip()
    if think_blocks:
        log.info("level=%d thinking (think-tags):\n%s", level.n, think_blocks.strip())
    if reasoning_full:
        log.info("level=%d thinking (reasoning_content):\n%s", level.n, reasoning_full)
    log.info("level=%d reply: %s", level.n, final)

    won = detect_secret(final, level.secret)
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
        final = prefix + final

    try:
        await placeholder.edit(content=trim_reply(final))
    except discord.HTTPException:
        # Final edit failed (rate limit, or user deleted the original message /
        # placeholder, which returns 50001 Missing Access). Try a fresh reply;
        # if that also fails, just log -- we've already logged the reply above.
        try:
            await message.reply(trim_reply(final), mention_author=False)
        except discord.HTTPException:
            log.warning("could not deliver final reply (message/channel gone?)")


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
