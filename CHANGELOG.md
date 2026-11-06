# Changelog

## 0.3.0: tasks, PRs, CI, schedules
- `/task`: background agy runs on their own branch in a git worktree, in parallel. Replies steer them. Diff, Merge, Open PR, and Discard when done. `/tasks`
- `/pr`: push and open a draft PR (gh CLI, or a compare link). CI is watched after `/push` and `/pr`, with a result ping and a "fix CI" button that sends the failed logs. `/ci`
- Plan approval: `/plan` replies get "✅ Implement this plan"
- `/at` and `/every` scheduled prompts, managed with `/schedules`
- `/queue` (cancel or reorder) and "⚡ Stop current & run this" on queued messages
- `/sessions`, a conversation picker with titles
- Buttons for `/mode` and `/effort`
- 📎 buttons to download files the reply mentions
- `VIEWER_USER_IDS`: read-only teammates
- The `/pr` prompt shortcut is renamed to `/prdesc`

## 0.2.0: developer toolkit
- Per-turn git checkpoints: every reply shows what agy changed, with Diff, Undo and Retry buttons
- `/projects`, `/p`, `/cwd -`: switch between repos with one tap. Each project keeps its own agy conversation.
- Git from chat: `/git`, `/diff [file|staged|turn]`, `/log`, `/branches`, `/undo`, `/commit`, `/push`, `/pull`
- `/test` with auto-detected runners and a "🛠 Ask agy to fix" button on failure; `/settest` per project
- Optional direct shell: `/sh` and `!cmd` (`ALLOW_SHELL`)
- Files: `/cat` with line ranges, `/get`, `/tree`, `/find`, `/grep`
- Approvals from Telegram: retry with full access for one turn, or "Always allow" a rule. `/allow` manages agy's settings.json allow list.
- Prompt shortcuts (`/review`, `/fix`, `/plan`, `/commitmsg`, `/pr`, `/debug`, …) plus custom ones in `prompts.toml`
- Voice notes, through local faster-whisper or OpenAI
- Forum topics: one independent agy session per topic
- Stop button on the live status, quiet status updates, and a notification only for the final reply
- `/retry`, `/last`, `/export` transcripts, `/logs`, `/restart`, `/update`

## 0.1.0
- First release: Telegram ↔ agy stream-json bridge
