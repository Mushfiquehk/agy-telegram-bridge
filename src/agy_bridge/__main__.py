"""Entry point: `python -m agy_bridge` (or the `agy-bridge` script)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import Config, ConfigError


def setup_logging(verbose: bool) -> None:
    Path("data").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = RotatingFileHandler("data/bridge.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # httpx logs every long-poll request at INFO, which is noisy and includes the bot token in URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def doctor(config: Config) -> int:
    """Check that agy can be found and answers a headless prompt."""
    from .agy import AgyNotFound, AgySession, resolve_command

    ok = True
    try:
        argv = resolve_command(config.agy_command)
        print(f"[ok] agy resolved: {argv}")
    except AgyNotFound as exc:
        print(f"[fail] {exc}")
        return 1
    print(f"[..] workspace: {config.workdir} (exists: {config.workdir.is_dir()})")
    if not config.workdir.is_dir():
        ok = False
    print("[..] sending a test prompt through stream-json (can take ~10-30s)...")
    session = AgySession(config.agy_command, config.workdir, persistent=False)
    response, error = "", ""
    async for ev in session.ask("Reply with exactly the word: pong", timeout=180):
        if ev.kind == "result":
            response, error = ev.response.strip(), ev.error
            print(f"[{'ok' if ev.ok else 'fail'}] status={ev.status} conversation={ev.conversation_id}")
        elif ev.kind == "error":
            error = ev.error
    if response:
        print(f"[ok] agy replied: {response!r}")
    if error:
        ok = False
        print(f"[fail] {error}")
        if "auth" in error.lower():
            print("       Run `agy` once interactively in a terminal to sign in, then retry.")
    print("[ok] telegram token present" if config.telegram_token else "[fail] no token")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agy-bridge", description="Talk to Antigravity CLI over Telegram.")
    parser.add_argument("--env", default=".env", help="path to the .env file (default: ./.env)")
    parser.add_argument("--doctor", action="store_true", help="check agy works headlessly, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"agy-bridge {__version__}")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env(dotenv_path=args.env)
    except ConfigError as exc:
        print(f"Config error: {exc}\nEdit {args.env} (copy .env.example to start).", file=sys.stderr)
        return 2

    if args.doctor:
        return asyncio.run(doctor(config))

    setup_logging(args.verbose)
    from .bot import run

    return run(config)


if __name__ == "__main__":
    sys.exit(main())
