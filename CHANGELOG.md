# Changelog

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
