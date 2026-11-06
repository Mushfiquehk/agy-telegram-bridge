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

## Developing from your phone

A typical loop:

1. `/projects` and tap a repo. Each project keeps its **own agy conversation**, so switching back resumes where you left off.
2. Type the task: *"add rate limiting to the /login route and a test for it"*. You see each step live (`⚡ run_command: npm test`, `✏️ edit_file: src/auth.ts`) with a **⏹ Stop** button.
3. The reply ends with **📝 3 files changed (+42 −7): src/auth.ts, …** and buttons: **📄 Diff** (exactly what agy changed in that turn), **↩️ Undo** (put the files back), **🔁 Retry**.
4. `/test`: runs the project's tests (auto-detected). If they fail, tap **🛠 Ask agy to fix** and the failing output goes straight to agy.
5. `/review` for a second opinion on the diff, then `/commit <msg>` (or `/commitmsg` to let agy write it) and `/push`, both behind a confirm button.

More things that help:

- **Approvals from Telegram.** Headless agy can't prompt you, so a blocked tool shows **✅ Retry with full access (this turn only)** and **📌 Always allow command(npm)**, which writes the rule into agy's `settings.json` (with a backup) and retries.
- **Screenshots and logs.** Send a screenshot of a bug or a log file with a caption and agy gets the file path. Uploads go to `.telegram_uploads/` with its own `.gitignore`, so they never end up in your commits.
- **Voice notes.** Dictate a task while walking. Transcribed locally with faster-whisper (free, private) or through OpenAI if you set a key.
- **Parallel work.** Make a Telegram group with **Topics** turned on, add your bot as admin, and each topic gets its own agy process and project: one topic per repo, all running at once.
- **Shortcuts.** `/review`, `/fix`, `/explain <x>`, `/plan <task>` (runs in plan mode, so no edits), `/commitmsg`, `/pr`, `/debug <error>`, `/tests <x>`, `/refactor <x>`, `/deps`. Add your own in `prompts.toml` (see [`prompts.example.toml`](prompts.example.toml)). Reply to any message with a shortcut to include that message.
- **Remote upkeep.** `/logs` shows the bridge log, `/restart` restarts it, and `/update` pulls the latest bridge from git, reinstalls, and restarts. These need `run.ps1` or the autostart task, which restart it automatically.

## Commands

| Command | What it does |
|---|---|
| *(any text)* | Sent to agy as the next turn. Reply to an earlier message to quote it as context. |
| *(file / photo + caption)* | Saved to `<project>/.telegram_uploads/`; the caption goes to agy with the path |
| *(voice note)* | Transcribed and sent to agy (see Voice notes in `.env.example`) |
| **Projects** | |
| `/projects` | Buttons for every repo under `ALLOWED_ROOTS` (recent first); `/projects refresh` rescans |
| `/p <name>` | Switch project by (partial) name |
| `/cwd [path\|-]` | Show or switch folder; `-` goes back to the previous project |
| `/ls [path]`, `/tree [depth]` | List a folder, or a `.gitignore`-aware tree |
| `/cat <file> [from-to]`, `/get <file>` | Show a file with line numbers, or download it |
| `/find <name>`, `/grep <regex>` | Find files by name; search contents (`git grep`, falling back to `rg`) |
| **Git** | |
| `/git` | Branch, ahead/behind, staged/modified/untracked files, last commit |
| `/diff [file\|staged\|turn]` | Working-tree diff (inline, or a `.diff` file when long); `turn` = the last agy turn only |
| `/undo` | Revert the last agy turn's file changes (asks first; won't rewrite commits) |
| `/log [n]`, `/branches` | Recent commits; branches by recency |
| `/commit <msg>`, `/push`, `/pull` | Commit all, push (sets upstream if needed), fast-forward pull. Commit and push ask first. |
| **Run** | |
| `/test [cmd]` | Run tests (`/settest` per project, `TEST_COMMAND`, or auto-detect). Failures get a fix button. |
| `/sh <cmd>`, `!<cmd>` | Run a shell command in the project (only with `ALLOW_SHELL=true`) |
| **Shortcuts** | `/review /fix /explain /plan /commitmsg /pr /debug /tests /refactor /deps` + yours; `/prompts` lists them |
| **Session** | |
| `/new` | Fresh conversation for this project |
| `/stop` | Interrupt the running turn and drop queued messages (the conversation is kept) |
| `/retry`, `/last` | Rerun the last prompt; resend the last reply |
| `/status` | Working/idle, project, branch, conversation, model, mode, permissions, test command |
| `/resume <id>`, `/history` | Jump back to an earlier conversation |
| `/export` | Transcript of this chat as Markdown (`/export this` = current conversation only) |
| **Settings** | |
| `/model [slug]`, `/models` | Show/set the model, or list them |
| `/effort [low\|medium\|high\|max\|default]`, `/mode [default\|accept-edits\|auto\|plan]` | Reasoning effort, execution mode |
| `/allow [rule]` | List agy's allow rules, add one (`/allow command(npm)`), or remove one (`/allow -command(npm)`) |
| `/yolo on\|off` | Toggle `--dangerously-skip-permissions` (only if `ALLOW_YOLO_TOGGLE=true`) |
| **Bridge** | `/usage`, `/logs`, `/restart`, `/update`, `/whoami` |

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
- `/cwd`, `/projects`, `/cat`, `/get`, and `/ls` can't leave `ALLOWED_ROOTS`.
- Direct shell (`/sh`, `!cmd`) is **off** unless you set `ALLOW_SHELL=true`. `/commit`, `/push`, and `/undo` always ask for a tap first.
- Buttons only work for allowed users, and they expire after a while.
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
| Diff/Undo buttons don't appear | The project isn't a git repo with at least one commit, or `CHECKPOINTS=false` |
| Voice notes say a transcriber is needed | `.\.venv\Scripts\pip install faster-whisper`, then `/restart` |
| Topics all share one agy | Turn on Topics in the group settings and make the bot an admin; each topic then gets its own session |
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
