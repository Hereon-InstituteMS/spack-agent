"""Unified command-line interface for spack-agent."""

import argparse
import sys

from .config import DEFAULT_CONFIG_PATH, describe_config, load_config
from .copilot import COPILOT_BIN, is_confined_snap_copilot
from .runner import run_workflow
from .session import SessionLockedError, SessionStore
from .workflow import process_result, show_status


def _load(path: str):
    try:
        return load_config(path)
    except ValueError as exc:
        raise SystemExit(f"[spack-agent] {exc}") from exc


def _preflight() -> None:
    if is_confined_snap_copilot():
        raise SystemExit(
            f"[spack-agent] '{COPILOT_BIN}' is Snap-confined. Install the official "
            "GitHub Copilot CLI or set COPILOT_CLI_PATH."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spack-agent", description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="iterate agent planning and build verification until completion"
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help="continue an unfinished session instead of starting fresh",
    )
    result = commands.add_parser("result", help="process a completed build log")
    result.add_argument("log", nargs="?")
    commands.add_parser("status", help="show the configured session")
    commands.add_parser("reset", help="clear session state but keep build logs")
    commands.add_parser("config", help="show resolved repositories and path roles")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = _load(args.config)
    store = SessionStore(config.state_dir)
    if args.command == "config":
        print(describe_config(config))
        return 0
    try:
        with store.lock():
            if args.command == "status":
                return show_status(store)
            if args.command == "reset":
                store.clear()
                print(f"[spack-agent] Session cleared; artifacts remain in {config.state_dir}.")
                return 0
            _preflight()
            if args.command == "result":
                return process_result(config, store, args.log)
            return run_workflow(config, args.resume)
    except SessionLockedError as exc:
        print(f"[spack-agent] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())