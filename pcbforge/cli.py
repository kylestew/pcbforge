"""Public command-line interface for pcbforge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pcbforge.initialize import InitError, InitInputError, initialize_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge",
        description="AI-assisted circuit-as-code PCB project tooling.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser(
        "init",
        help="scaffold a project from spec.md",
        description=(
            "Create a pcbforge project from spec.md. This command is create-only "
            "and never overwrites an existing scaffold."
        ),
    )
    init_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="existing directory containing spec.md (default: current directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "init":
        try:
            result = initialize_project(Path(args.project_dir))
        except InitInputError as exc:
            print(f"pcbforge init: {exc}", file=sys.stderr)
            return 2
        except InitError as exc:
            print(f"pcbforge init: {exc}", file=sys.stderr)
            return 1

        print(f"pcbforge: initialized {result.name} in {result.project_dir}")
        print(
            "pcbforge: compiler smoke test passed; "
            "next phase: propose the module architecture"
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
