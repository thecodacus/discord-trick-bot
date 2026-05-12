# trick-the-AI Discord bot

A Discord bot that runs a jailbreak game: users in a designated channel try to manipulate the bot into revealing a forbidden secret word. If they succeed, they get a role for that level.

Inspired by [Lakera Gandalf](https://gandalf.lakera.ai/). Bot replies are powered by your homelab's llama.cpp server.

## How it works

- Each **level** has a (secret word, role, system-prompt) tuple.
- A user in the configured channel sends a prompt → bot forwards to llama-server with the level's system prompt → bot replies in-thread.
- If the reply contains the secret word (case-insensitive substring), the user is awarded the level's role.
- `/level <n>` switches which level the user is attempting. `/status` shows cleared levels.
- Stateless: each message is a fresh single-turn chat. The game is one prompt at a time.

## Setup

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications → New Application.
2. **Bot** tab → Reset Token → copy the token (this is `DISCORD_TOKEN`).
3. Enable **Privileged Gateway Intents**:
   - `MESSAGE CONTENT INTENT` (needed to read user messages)
   - `SERVER MEMBERS INTENT` (needed to grant roles)
4. **OAuth2 → URL Generator** → scopes: `bot`, `applications.commands`; bot permissions: `Send Messages`, `Manage Roles`, `Read Message History`. Invite the bot to your server using the generated URL.
5. In Discord, with **Developer Mode** on (Settings → Advanced), right-click the channel → Copy Channel ID. Same for the server (Copy Server ID) and each level's role.
6. **IMPORTANT for role granting**: in Server Settings → Roles, drag the bot's auto-created role **above** the level roles. Discord rejects role assignments when the target role is at or above the bot's highest role.

### 2. Configure

```sh
cp .env.example .env
$EDITOR .env
```

Fill in `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `LLAMA_BASE_URL`, and one block per level. Per-level prompts go in `prompts/level_N.txt` — `{SECRET}` is substituted at load time.

### 3. Run

```sh
docker compose up -d --build
docker compose logs -f trick-bot
```

The bot connects to llama-server at `LLAMA_BASE_URL` (default `http://localhost:8080`). With `network_mode: host`, `localhost` resolves to the homelab box's own services.

For ad-hoc dev without Docker:

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Operating

- `/level <n>` – switch attempt level.
- `/status` – see which levels you've cleared.
- Any plain message in the configured channel is treated as an attempt at your current level.
- A correct trick: 🎉 message + role granted.
- An unsuccessful trick: just the bot's in-character refusal.

## Adding levels

1. Pick a secret word.
2. Create a Discord role (Server Settings → Roles).
3. Write `prompts/level_N.txt` — use `{SECRET}` where the bot needs to know the secret. The defending instructions get stronger as N goes up.
4. Append the `LEVEL_N_*` triple to `.env`.
5. Restart.

The bot auto-discovers levels by scanning `LEVEL_1_*`, `LEVEL_2_*`, … until a gap.

## What v1 doesn't do (yet)

- **Multi-turn conversations.** Each attempt is one prompt. Adding multi-turn is a thread-scoped chat history; not hard, but changes the game's character.
- **Cooldowns / rate-limits.** Spamming the bot is currently unlimited. Add per-user cooldowns if abuse appears.
- **Leaderboard.** No persistence of attempts beyond Discord's role state.
- **Judge model.** Detection is plain substring match on the secret. If someone tricks the bot into describing the secret without saying the word verbatim, that's currently a loss for the user. A second LLM call ("did the assistant effectively reveal the secret?") would catch this — at 2× inference cost.

## License

MIT. Have fun.
