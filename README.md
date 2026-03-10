# ClassNotifierThing

**Never miss an open seat again.** ClassNotifierThing watches UMass SPIRE for the classes you care about and pings you on Discord the moment a seat or waitlist spot opens—so you can register before it’s gone.

Built for students tired of refreshing SPIRE every few minutes. Run it on your laptop or host the multi-user Discord bot for a small group. Checks run on a timer in the background; Discord only gets notified when something actually changes.

> Unofficial project — not affiliated with UMass. Use responsibly and in line with university policies.

**Stack:** Python · [Playwright](https://playwright.dev/python/) (Chromium) · Discord webhooks / bot API

## Features

- Watch multiple classes by `class_nbr` and `crse_id`
- Notify only on real changes (seat open, waitlist open, session expired) — not every poll
- **Single-user mode**: one `config.json` + optional webhook
- **Multi-user bot mode**: `/signup`, private channels, per-user watcher processes, OTP login via Discord

## Requirements

- Python 3.10+
- macOS or Linux
- UMass SPIRE account (Microsoft login + DUO)
- Discord webhook and/or bot token

---

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/ClassNotifierThing.git
cd ClassNotifierThing

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Configuration files (copy examples — do not commit real configs)

```bash
cp config.example.json config.json
# For bot mode only:
cp bot_config.example.json bot_config.json
```

| File                         | Gitignored | Contains                                    |
| ---------------------------- | ---------- | ------------------------------------------- |
| `config.json`                | Yes        | Classes, webhook, SPIRE/Discord credentials |
| `bot_config.json`            | Yes        | Bot token, guild, channel IDs               |
| `session.json` / `sessions/` | Yes        | SPIRE login cookies                         |
| `configs/`                   | Yes        | Per-user configs in bot mode                |

---

## Single-user mode

### 1. Edit `config.json`

```json
{
  "name": "Your Name",
  "discord_webhook_url": "https://discord.com/api/webhooks/...",
  "check_interval": 300,
  "term": "1267",
  "institution": "UMAMH",
  "classes": [
    {
      "class_nbr": "11706",
      "crse_id": "042327"
    }
  ]
}
```

Optional fields for Discord-assisted login when the session expires:

- `discord_bot_token`
- `discord_channel_id`
- `spire_username` / `spire_password`

### 2. Find class IDs

Open the class in SPIRE and copy from the URL:

```text
...CRSE_ID=042327...CLASS_NBR=11706...
```

### 3. Log in

```bash
python3 script.py --login
```

Browser opens → log into SPIRE + DUO → press **Enter** in the terminal. Saves `session.json`.

### 4. Run

```bash
python3 script.py
```

On macOS, keep the machine awake:

```bash
caffeinate -i python3 script.py
```

---

## Multi-user Discord bot mode

### Discord app setup

1. Create an app at [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a bot and copy the **token**.
3. Enable **Message Content Intent**.
4. Invite the bot with:
   - Read/send messages (signup channel + private channels)
   - **Manage Channels** (creates per-user private channels)
   - Access to your target category

### Edit `bot_config.json`

```json
{
  "bot_token": "YOUR_BOT_TOKEN",
  "guild_id": "YOUR_SERVER_ID",
  "signup_channel_id": "PUBLIC_SIGNUP_CHANNEL_ID",
  "category_id": "CATEGORY_FOR_PRIVATE_CHANNELS",
  "term": "1267",
  "institution": "UMAMH"
}
```

### Run the bot

```bash
python3 script.py --bot
```

Restarts watchers for all users in `configs/` who have classes configured.

### Signup flow

1. User sends `/signup` in the signup channel.
2. User replies with UMass email → bot creates a **private channel**.
3. User sends SPIRE password in the private channel.
4. Bot logs in; user sends `!otp 123456` if prompted.
5. User runs `/add CLASS_NBR` for each class, then `/done`.
6. A background watcher process starts for that user.

### Commands (private channel)

| Command             | Description                               |
| ------------------- | ----------------------------------------- |
| `/add CLASS_NBR`    | Look up class in SPIRE, add to watch list |
| `/remove CLASS_NBR` | Remove from watch list                    |
| `/classes`          | List watched classes                      |
| `/watch`            | Start watcher                             |
| `/kill`             | Stop watcher                              |
| `/status`           | Watcher status                            |
| `/login`            | Re-authenticate SPIRE                     |
| `/resend`           | Retry auth / OTP flow                     |
| `/logout`           | Delete saved session                      |
| `/help`             | List commands                             |
| `!otp CODE`         | Submit DUO/OTP during login               |

---

## Notifications

Discord is notified when:

- A **seat** opens
- A **waitlist** spot opens
- The SPIRE **session** expires (re-login needed)

Normal checks only print to the terminal (or `logs/watcher_*.log` in bot mode).

---

## Project layout

```text
ClassNotifierThing/
├── script.py                 # Main watcher + Discord bot
├── config.example.json       # Template (safe to commit)
├── bot_config.example.json   # Template (safe to commit)
├── requirements.txt
├── configs/                  # Per-user configs (created at runtime, gitignored)
├── sessions/                 # Per-user SPIRE sessions (gitignored)
└── logs/                     # Watcher logs (gitignored)
```

## Environment variables

| Variable              | Description                                    |
| --------------------- | ---------------------------------------------- |
| `CONFIG_FILE`         | Path to config JSON (set per user in bot mode) |
| `WATCHER_NAME`        | Username for logs / PID files                  |
| `DISCORD_WEBHOOK_URL` | Override webhook from config                   |

---

## Security (read before pushing)

- **Never commit** `config.json`, `bot_config.json`, `session.json`, `sessions/`, or `configs/`.
- Copy from `*.example.json` locally after cloning.
- If a bot token or password was ever committed or shared, **rotate it** in the Discord developer portal and change your SPIRE password.
- Bot mode stores SPIRE passwords in `configs/` on disk — run only on a machine you trust.
- Unofficial tool; not affiliated with UMass. Use responsibly.

## Disclaimer

Enrollment data comes from SPIRE and can change between checks. No guarantee you will get a seat.

## License

MIT — see [LICENSE](LICENSE) if present, or add your own.
