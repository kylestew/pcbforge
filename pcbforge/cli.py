"""Public command-line interface for pcbforge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pcbforge.initialize import InitError, InitInputError, initialize_project
from pcbforge.ioc import (
    IocProjectError,
    IocValidationError,
    check_ioc,
)
from pcbforge.status import (
    StatusCheckError,
    StatusError,
    StatusInputError,
    inspect_status,
    mark_status,
    read_status_document,
    render_terminal,
    run_status_checks,
    write_status,
)


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

    check_ioc_parser = subcommands.add_parser(
        "check-ioc",
        help="validate the project's canonical STM32CubeMX configuration",
        description=(
            "Validate firmware/<project>.ioc against spec.md, then use the "
            "pinned STM32CubeMX 6.18 command-line mode for a non-mutating "
            "semantic round-trip check."
        ),
    )
    check_ioc_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )

    status_parser = subcommands.add_parser(
        "status",
        help="show or update the project's tracked workflow dashboard",
        description=(
            "Show status from project evidence, refresh STATUS.md, or record "
            "an explicit workflow milestone."
        ),
    )
    status_parser.add_argument(
        "status_args",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    return parser


def _status_show_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge status",
        description=(
            "Show the current workflow phase. Static inspection is read-only; "
            "--check runs applicable pinned validators and --write refreshes "
            "the tracked STATUS.md dashboard."
        ),
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="project containing spec.md (default: current directory)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="create or refresh STATUS.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run stage-appropriate compiler, IOC, and DRC checks",
    )
    return parser


def _status_mark_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge status mark",
        description=(
            "Append a durable workflow event and refresh STATUS.md. Completion "
            "is accepted only when predecessors and required evidence are current."
        ),
    )
    parser.add_argument("phase", help="workflow phase key, such as layout or order")
    parser.add_argument(
        "action",
        help="complete, blocked, reopened, or skipped",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="project containing spec.md (default: current directory)",
    )
    parser.add_argument(
        "--note",
        required=True,
        help="short explanation recorded in the append-only event history",
    )
    return parser


def _run_status_cli(argv: list[str]) -> int:
    is_mark = bool(argv and argv[0] == "mark")
    parser = _status_mark_parser() if is_mark else _status_show_parser()
    status_args = parser.parse_args(argv[1:] if is_mark else argv)
    try:
        if is_mark:
            result = mark_status(
                Path(status_args.project_dir),
                status_args.phase,
                status_args.action,
                status_args.note,
            )
            print(render_terminal(result.report))
            print(
                f"pcbforge: recorded {status_args.phase} {status_args.action}; "
                f"{'updated' if result.wrote else 'unchanged'} STATUS.md"
            )
            return 0

        if status_args.write:
            result = write_status(
                Path(status_args.project_dir),
                check=status_args.check,
            )
            report = result.report
            print(render_terminal(report))
            print(
                "pcbforge: "
                f"{'updated' if result.wrote else 'unchanged'} STATUS.md"
            )
        else:
            project_dir = Path(status_args.project_dir)
            document = read_status_document(project_dir.expanduser().resolve())
            if status_args.check:
                document = run_status_checks(project_dir, document)
            report = inspect_status(project_dir, document=document)
            print(render_terminal(report))
        return 1 if status_args.check and report.checks_failed else 0
    except StatusInputError as exc:
        print(f"pcbforge status: {exc}", file=sys.stderr)
        return 2
    except StatusCheckError as exc:
        print(f"pcbforge status: {exc}", file=sys.stderr)
        return 1
    except StatusError as exc:
        print(f"pcbforge status: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["status"]:
        return _run_status_cli(raw_argv[1:])

    args = _parser().parse_args(raw_argv)

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
            "STATUS.md refreshed; run `pcbforge status` for the next action"
        )
        return 0

    if args.command == "check-ioc":
        try:
            result = check_ioc(Path(args.project_dir))
        except (IocProjectError, InitInputError) as exc:
            print(f"pcbforge check-ioc: {exc}", file=sys.stderr)
            return 2
        except IocValidationError as exc:
            print(f"pcbforge check-ioc: {exc}", file=sys.stderr)
            return 1

        print(
            f"pcbforge: valid {result.part_number} "
            f"({result.family}, {result.package})"
        )
        print("pin\tlabel\tsignal\tmode")
        for pin in result.pins:
            print(
                "\t".join(
                    (
                        pin.pin,
                        pin.label or "-",
                        pin.signal,
                        pin.mode or "-",
                    )
                )
            )
        print("pcbforge: CubeMX 6.18 semantic round-trip passed")
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
