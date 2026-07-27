"""Public command-line interface for pcbforge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pcbforge.build_test import (
    BuildTestError,
    BuildTestInputError,
    check_build_test,
)
from pcbforge.initialize import InitError, InitInputError, initialize_project
from pcbforge.ioc import (
    IocProjectError,
    IocValidationError,
    check_ioc,
)
from pcbforge.parts import (
    PartsAuditError,
    PartsAuditInputError,
    check_parts,
    render_parts_audit,
)
from pcbforge.policy import (
    PolicyError,
    PolicyInputError,
    check_policy,
    migrate_policy,
    render_policy_result,
)
from pcbforge.placement import (
    PlacementError,
    PlacementInputError,
    check_brief,
    generate_brief,
)
from pcbforge.status import (
    StatusCheckError,
    StatusError,
    StatusInputError,
    inspect_status,
    mark_policy,
    mark_status,
    policy_approval_context,
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

    check_parts_parser = subcommands.add_parser(
        "check-parts",
        help="block project-local KiCad assets for commodity parts",
        description=(
            "Audit src/parts and reject generated project-local symbols, "
            "footprints, or models for standard chip resistors, capacitors, "
            "and LEDs that have canonical KiCad library assets."
        ),
    )
    check_parts_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )

    check_build_test_parser = subcommands.add_parser(
        "check-build-test",
        help="run the deterministic Step 6 acceptance gate",
        description=(
            "Run a pinned frozen build, then validate build-test.yaml against "
            "the exact BOM, source assertions, resolved PCB, and no-op spatial "
            "preservation contract."
        ),
    )
    check_build_test_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )
    check_build_test_parser.add_argument(
        "--write-report",
        action="store_true",
        help="atomically write docs/build-test.md after a full pass",
    )

    brief_parser = subcommands.add_parser(
        "brief",
        help="generate the Step 7 placement brief and KiCad net classes",
        description=(
            "Validate placement.yaml against the current Step 6 PCB topology, "
            "generate brief.md, and merge only PCBForge-owned net classes into "
            "the KiCad project. Never changes the PCB."
        ),
    )
    brief_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )

    check_brief_parser = subcommands.add_parser(
        "check-brief",
        help="validate the current Step 7 placement outputs without writing",
        description=(
            "Read-only validation of placement.yaml, brief.md, the current "
            "non-spatial PCB topology, and PCBForge-owned KiCad net classes."
        ),
    )
    check_brief_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )

    check_policy_parser = subcommands.add_parser(
        "check-policy",
        help="validate manufacturing and technology policy",
        description=(
            "Read-only validation of the pinned schema-10 platform policy, "
            "project declarations, sourcing evidence, and approved exceptions."
        ),
    )
    check_policy_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="pcbforge project (default: current directory)",
    )

    policy_parser = subcommands.add_parser(
        "policy",
        help="record explicit project-policy approvals",
    )
    policy_commands = policy_parser.add_subparsers(
        dest="policy_command",
        required=True,
    )
    baseline_parser = policy_commands.add_parser(
        "approve-baseline",
        help="record explicit approval of a migrated policy baseline",
    )
    baseline_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
    )
    baseline_parser.add_argument("--note", required=True)

    exception_parser = policy_commands.add_parser(
        "approve-exception",
        help="record explicit approval of one declared policy exception",
    )
    exception_parser.add_argument("exception_id", metavar="EXCEPTION_ID")
    exception_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
    )
    exception_parser.add_argument("--note", required=True)

    sourcing_parser = policy_commands.add_parser(
        "confirm-sourcing",
        help="record the post-FAB pre-order sourcing review",
    )
    sourcing_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
    )
    sourcing_parser.add_argument("--note", required=True)

    migrate_parser = subcommands.add_parser(
        "migrate-policy",
        help="explicitly migrate a schema-7-through-9 project to schema 10",
    )
    migrate_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized schema-7-through-9 project (default: current directory)",
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
        help=(
            "run stage-appropriate compiler, policy, build-test, parts, "
            "placement-brief, IOC, and DRC checks"
        ),
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
        help="complete, proposal-approved, blocked, reopened, or skipped",
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
            print(f"pcbforge: {'updated' if result.wrote else 'unchanged'} STATUS.md")
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
            f"pcbforge: valid {result.part_number} ({result.family}, {result.package})"
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

    if args.command == "check-parts":
        try:
            result = check_parts(Path(args.project_dir))
        except PartsAuditInputError as exc:
            print(f"pcbforge check-parts: {exc}", file=sys.stderr)
            return 2
        except PartsAuditError as exc:
            print(f"pcbforge check-parts: {exc}", file=sys.stderr)
            return 1

        print(render_parts_audit(result))
        return 0 if result.ok else 1

    if args.command == "check-policy":
        try:
            project_dir = Path(args.project_dir)
            document = read_status_document(project_dir.expanduser().resolve())
            report = inspect_status(project_dir, document=document)
            through_phase = (
                report.current.phase.key
                if report.current is not None
                else "verify"
            )
            baseline, exceptions, _ = policy_approval_context(document)
            result = check_policy(
                project_dir,
                through_phase=through_phase,
                baseline_approval=baseline,
                exception_approvals=exceptions,
            )
        except PolicyInputError as exc:
            print(f"pcbforge check-policy: {exc}", file=sys.stderr)
            return 2
        except PolicyError as exc:
            print(f"pcbforge check-policy: {exc}", file=sys.stderr)
            return 1

        print(render_policy_result(result))
        return 0 if result.ok else 1

    if args.command == "policy":
        action = {
            "approve-baseline": "baseline-approved",
            "approve-exception": "exception-approved",
            "confirm-sourcing": "sourcing-confirmed",
        }[args.policy_command]
        try:
            result = mark_policy(
                Path(args.project_dir),
                action,
                args.note,
                subject=getattr(args, "exception_id", ""),
            )
        except (StatusInputError, PolicyInputError) as exc:
            print(f"pcbforge policy: {exc}", file=sys.stderr)
            return 2
        except (StatusError, PolicyError) as exc:
            print(f"pcbforge policy: {exc}", file=sys.stderr)
            return 1
        print(render_terminal(result.report))
        print(
            f"pcbforge: recorded policy {action}; "
            f"{'updated' if result.wrote else 'unchanged'} STATUS.md"
        )
        return 0

    if args.command == "migrate-policy":
        try:
            migration = migrate_policy(Path(args.project_dir))
            if migration.wrote:
                write_status(Path(args.project_dir))
        except PolicyInputError as exc:
            print(f"pcbforge migrate-policy: {exc}", file=sys.stderr)
            return 2
        except (PolicyError, StatusError) as exc:
            print(f"pcbforge migrate-policy: {exc}", file=sys.stderr)
            return 1
        state = "migrated" if migration.wrote else "already migrated"
        print(f"pcbforge: {state} policy in {migration.project_dir}")
        if migration.review_items:
            print(
                "pcbforge: review required for "
                + ", ".join(migration.review_items)
            )
        if migration.wrote:
            print(
                "pcbforge: explicit policy baseline approval is still required"
            )
        return 0

    if args.command == "check-build-test":
        try:
            result = check_build_test(
                Path(args.project_dir),
                write_report=args.write_report,
            )
        except BuildTestInputError as exc:
            print(f"pcbforge check-build-test: {exc}", file=sys.stderr)
            return 2
        except BuildTestError as exc:
            print(f"pcbforge check-build-test: {exc}", file=sys.stderr)
            return 1

        print(f"pcbforge: build + test passed — {result.summary}")
        if args.write_report:
            state = "updated" if result.wrote_report else "unchanged"
            print(f"pcbforge: {state} {result.report_path.as_posix()}")
        return 0

    if args.command in {"brief", "check-brief"}:
        try:
            result = (
                generate_brief(Path(args.project_dir))
                if args.command == "brief"
                else check_brief(Path(args.project_dir))
            )
        except PlacementInputError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 2
        except PlacementError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 1

        print(f"pcbforge: placement brief passed — {result.summary}")
        if args.command == "brief":
            brief_state = "updated" if result.wrote_brief else "unchanged"
            project_state = "updated" if result.wrote_project else "unchanged"
            print(
                f"pcbforge: {brief_state} {result.brief_path.as_posix()}; "
                f"{project_state} {result.project_path.as_posix()}; PCB unchanged"
            )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
