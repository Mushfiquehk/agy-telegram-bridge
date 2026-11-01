# agy-telegram-bridge

Talk to the **Google Antigravity CLI (`agy`)** running on your Windows 11 PC from **Telegram** on your phone. Every message you send becomes a turn in a real `agy` conversation in your project folder; you see tool calls live, then the agent's reply, formatted.

```
 Telegram app ──► Telegram Bot API ◄── long-poll ── bridge (Python, your PC) ──stdin/stdout──► agy
   (phone)                                          no open ports, no webhook        stream-json
```

## Why Telegram and not WhatsApp

| | Telegram | WhatsApp |
|---|---|---|
| Setup | 2 minutes: `/newbot` in @BotFather, copy a token | Meta developer account, business app, phone number, verified webhook |
| Networking | Long polling: works behind your home router, nothing exposed | Cloud API needs a public HTTPS webhook (tunnel/ngrok) |
| Cost / risk | Free, official bot API | Official API has per-conversation pricing; unofficial QR libraries risk a ban on your number |
| Formatting, files, edits | Code blocks, live-edited status messages, file upload/download | Limited |

So this repo is Telegram-only on purpose. (The `core` module is messenger-agnostic, so a WhatsApp front end could be added later.)

## How it works

- Uses agy's documented **headless stream-json protocol**: `agy --input-format stream-json --output-format stream-json` ([docs](https://antigravity.google/docs/cli/headless)). One warm `agy` process per chat keeps context between messages, and the bridge parses `init` / `step_update` / `result` events.
- **Prompts go over stdin as JSON, never on the command line**, so `& | ^ " %PATH%` and friends can't be mangled or injected through `cmd.exe`.
- The conversation id is saved to `data/state.json`. If agy crashes, times out, idles out, or the bridge restarts, the next message resumes the same thread with `--conversation <id>`.
- While agy works you get one live status message (`⏳ Working… 42s`, then `⚡ run_command: npm test`, `✏️ write_to_file: src/app.ts`, …), edited in place and rate-limited. Then the final answer arrives as Telegram HTML; long answers are split safely (code fences are closed and reopened across messages) or attached as a `.md` file.
- Messages sent while a turn is running are **queued**; `/stop` interrupts.

## Quick start (Windows 11, native: recommended)

1. **Install and sign in to agy** (once, interactively): open a terminal and run `agy`, finish sign-in, then `/exit`. Headless mode reuses those cached credentials.
2. **Create the bot**: in Telegram, open **@BotFather**, send `/newbot`, pick a name, and copy the token.
3. **Get your user id**: message **@userinfobot** (or start the bridge and send `/whoami` to your bot).
4. **Set up the bridge**:
   ```powershell
   git clone https://github.com/Mushfiquehk/agy-telegram-bridge.git
   cd agy-telegram-bridge
   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1   # venv, deps, opens .env
   ```
   In `.env`, set `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, and `AGY_WORKDIR` (your project folder).
5. **Check agy works headlessly**:
   ```powershell
   .\.venv\Scripts\python.exe -m agy_bridge --doctor
   ```
6. **Run it**:
   ```powershell
   powershell -File scripts\run.ps1
   ```
   Open your bot in Telegram, press **Start**, and type a request.
7. *(Optional)* **Start at logon, hidden**: `powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1`. Set your PC not to sleep, or the bot goes offline.

## Commands

| Command | What it does |
|---|---|
| *(any text)* | Sent to agy as the next turn. Reply to an earlier message to quote it as context. |
| *(file / photo + caption)* | Saved to `<workspace>/.telegram_uploads/`, and the caption is sent to agy with the path |
| `/new` | Start a fresh conversation |
| `/stop` | Interrupt the running turn and drop queued messages (the conversation is kept) |
| `/status` | Working/idle, workspace, conversation id, model, mode, permissions, queue |
| `/cwd [path]` | Show or switch the workspace (restricted to `ALLOWED_ROOTS`; starts a new conversation) |
| `/ls` | List the workspace folder |
| `/model [slug]`, `/models` | Show/set the model, or list them (`agy models`) |
| `/effort [low\|medium\|high\|max\|default]` | Reasoning effort |
| `/mode [default\|accept-edits\|auto\|plan]` | Execution mode |
| `/yolo on\|off` | Toggle `--dangerously-skip-permissions` (only if `ALLOW_YOLO_TOGGLE=true`) |
| `/resume <id>`, `/history` | Jump back to an earlier conversation |
| `/usage` | `agy -p /usage` quota report |
| `/whoami` | Your Telegram user id (works for anyone; reveals nothing else) |

Settings changes restart agy with new flags but keep the conversation.

## Permissions: read this

Headless agy **cannot show you approval prompts**. Per the docs, file reads and writes inside the workspace are auto-allowed, but anything that needs approval (shell commands by default) is **soft-denied**: the turn continues without it, and agy prints a notice. The bridge forwards those notices to you as `⚠️ Permission notices`.

Pick a level:

1. **Scoped allow rules (recommended).** Copy [`examples/settings.json`](examples/settings.json) into `%USERPROFILE%\.gemini\antigravity-cli\settings.json` (merge with what's there) and adjust the `permissions.allow` list, e.g. `command(git)`, `command(regex:npm run (build|test))`.
2. **`AGY_MODE=accept-edits`** auto-approves workspace file edits.
3. **`AGY_MODE=auto`** with the terminal sandbox (agy v1.4.4+ on Windows): safe, reversible commands run without prompting.
4. **`AGY_SKIP_PERMISSIONS=true`** (or `/yolo on`): agy may run **any** command on your PC. Only do this with `ALLOWED_USER_IDS` set to you alone.

## Security model

- Only the Telegram user ids in `ALLOWED_USER_IDS` are served; everyone else is ignored and logged.
- Your bot token is effectively a password to your PC. Never commit `.env` (it's git-ignored). If it leaks, use `/revoke` in @BotFather.
- `/cwd` can't leave `ALLOWED_ROOTS`.
- No inbound ports; the bridge only makes outbound HTTPS calls to `api.telegram.org`.
- The bridge runs as your Windows user, so agy has your user's rights. Use a dedicated workspace folder.

## Running under WSL

Native Windows is simpler, because agy's sign-in, your projects, and Windows paths all line up. Use WSL only if you installed **agy inside WSL** (Linux build) and signed in there:

```bash
./scripts/setup-wsl.sh
.venv/bin/python -m agy_bridge --doctor
.venv/bin/python -m agy_bridge
```

For autostart with systemd enabled in WSL, create `~/.config/systemd/user/agy-bridge.service`:

```ini
[Unit]
Description=agy Telegram bridge
[Service]
WorkingDirectory=%h/agy-telegram-bridge
ExecStart=%h/agy-telegram-bridge/.venv/bin/python -m agy_bridge
Restart=always
[Install]
WantedBy=default.target
```
Then run `systemctl --user enable --now agy-bridge`.

## Configuration

All settings live in `.env`; see [`.env.example`](.env.example) for every option with comments. The highlights: `AGY_COMMAND` (full path if `agy` isn't on PATH), `RUN_MODE` (`persistent` for a warm process, or `oneshot` for a process per message, as a fallback), `TURN_TIMEOUT_SECONDS`, `IDLE_SHUTDOWN_SECONDS`, and `SHOW_TOOL_CALLS`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `authentication required` | Run `agy` interactively once and sign in, as the same Windows user the bridge runs as |
| `Could not find 'agy'` | Set `AGY_COMMAND` to the full path (`where agy` in cmd shows it) |
| Bot never answers | Check `data\bridge.log`; confirm your id with `/whoami`; make sure only **one** bridge runs per token |
| agy "did nothing" | It was probably soft-denied; see Permissions above |
| Weird behaviour with the persistent process | Set `RUN_MODE=oneshot` |
| `/model` errors | Use a slug from `/models`; headless agy fails loudly on unknown models |

## Development

```bash
pip install -e ".[dev]"
pytest -q
```
The tests drive a fake `agy` ([`tests/fake_agy.py`](tests/fake_agy.py)) that speaks the same stream-json protocol, covering multi-turn sessions, resume, tool events, crashes, timeouts, `/stop`, permission notices, long-message chunking, and HTML escaping. CI runs them on Windows and Ubuntu.

## Also worth knowing

Antigravity has its own **Remote Control** (`agy remote-control start`, or `agy --remote-control`), which lets you drive a CLI session from a browser. This bridge is for when you'd rather just text it from Telegram.

## License

MIT
