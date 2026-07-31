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
from pcbforge.compatibility import CompatibilityError, validate_project_compatibility
from pcbforge.circuit_review import (
    CircuitReviewError,
    CircuitReviewInputError,
    check_circuit_review,
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
    approve_phase,
    inspect_status,
    mark_policy,
    mark_status,
    policy_approval_context,
    read_status_document,
    record_initialization_blocker,
    render_next,
    render_phase_review,
    render_terminal,
    review_phase,
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

    check_circuit_review_parser = subcommands.add_parser(
        "check-circuit-review",
        help="validate the authored CIRCUIT review gate",
        description=(
            "Validate the exact proposal model and authored explanatory SVG. "
            "Final checks compare the approved model directly with the compiled "
            "Atopile BOM and PCB topology; KiCad schematic generation is not used."
        ),
    )
    check_circuit_review_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )
    check_circuit_review_parser.add_argument(
        "--stage",
        required=True,
        choices=("proposal", "final"),
    )
    check_circuit_review_parser.add_argument(
        "--write",
        action="store_true",
        help="atomically write canonical circuit review evidence",
    )

    check_build_test_parser = subcommands.add_parser(
        "check-build-test",
        help="run the deterministic CIRCUIT acceptance gate",
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

    prepare_layout_parser = subcommands.add_parser(
        "prepare-layout",
        help="prepare the CIRCUIT-to-LAYOUT handoff",
        description=(
            "Validate placement.yaml against the approved CIRCUIT topology, "
            "generate docs/placement-brief.md, and merge only PCBForge-owned "
            "net classes. Never changes the PCB."
        ),
    )
    prepare_layout_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )
    check_handoff_parser = subcommands.add_parser(
        "check-layout-handoff",
        help="validate the CIRCUIT-to-LAYOUT handoff without writing",
    )
    check_handoff_parser.add_argument(
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
            "Read-only validation of the pinned project platform policy, "
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
            "Show the current workflow handoff. Static inspection is read-only; "
            "--next shows the compact last/current/next view, --check runs "
            "applicable pinned validators, and --write refreshes the tracked "
            "STATUS.md dashboard."
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
            "layout-handoff, IOC, and DRC checks"
        ),
    )
    parser.add_argument(
        "--next",
        action="store_true",
        help="show only the last, current, and next workflow handoff",
    )
    return parser


def _status_mark_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge status mark",
        description=(
            "Append a non-approval workflow event and refresh STATUS.md. "
            "Phase completion uses `pcbforge status approve`."
        ),
    )
    parser.add_argument("phase", help="workflow phase key, such as layout or order")
    parser.add_argument(
        "action",
        help="blocked, reopened, or skipped",
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


def _status_review_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge status review",
        description=(
            "Run read-only phase checks and print the exact artifact/check "
            "packet and fingerprint that may be presented for user approval."
        ),
    )
    parser.add_argument("phase", help="workflow phase key, such as architect or circuit")
    parser.add_argument(
        "--stage",
        choices=("proposal", "handoff", "final"),
        default="final",
        help="review a proposal, the LAYOUT handoff, or a final phase packet",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="project containing spec.md (default: current directory)",
    )
    return parser


def _status_approve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge status approve",
        description=(
            "Record an explicit user approval of the exact fingerprint "
            "previously presented by `pcbforge status review`."
        ),
    )
    parser.add_argument("phase", help="workflow phase key, such as architect or circuit")
    parser.add_argument(
        "--stage",
        choices=("proposal", "handoff", "final"),
        default="final",
        help="approve a proposal, the LAYOUT handoff, or a final phase packet",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="project containing spec.md (default: current directory)",
    )
    parser.add_argument(
        "--fingerprint",
        required=True,
        help="exact SHA-256 printed by the phase review command",
    )
    parser.add_argument(
        "--note",
        required=True,
        help="the explicit user approval recorded in append-only history",
    )
    return parser


def _run_status_cli(argv: list[str]) -> int:
    mode = argv[0] if argv and argv[0] in {"mark", "review", "approve"} else "show"
    parser = {
        "mark": _status_mark_parser,
        "review": _status_review_parser,
        "approve": _status_approve_parser,
        "show": _status_show_parser,
    }[mode]()
    status_args = parser.parse_args(argv[1:] if mode != "show" else argv)
    try:
        validate_project_compatibility(Path(status_args.project_dir))
        if mode == "mark":
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
        if mode == "review":
            review = review_phase(
                Path(status_args.project_dir),
                status_args.phase,
                stage=status_args.stage,
            )
            print(render_phase_review(review))
            return 0 if review.ready else 1
        if mode == "approve":
            result = approve_phase(
                Path(status_args.project_dir),
                status_args.phase,
                status_args.fingerprint,
                status_args.note,
                stage=status_args.stage,
            )
            print(render_terminal(result.report))
            print(
                f"pcbforge: recorded explicit {status_args.phase} approval; "
                f"{'updated' if result.wrote else 'unchanged'} STATUS.md"
            )
            return 0

        if status_args.write:
            result = write_status(
                Path(status_args.project_dir),
                check=status_args.check,
            )
            report = result.report
            print(
                render_next(report)
                if status_args.next
                else render_terminal(report)
            )
            print(f"pcbforge: {'updated' if result.wrote else 'unchanged'} STATUS.md")
        else:
            project_dir = Path(status_args.project_dir)
            document = read_status_document(project_dir.expanduser().resolve())
            if status_args.check:
                document = run_status_checks(project_dir, document)
            report = inspect_status(project_dir, document=document)
            print(
                render_next(report)
                if status_args.next
                else render_terminal(report)
            )
        return 1 if status_args.check and report.checks_failed else 0
    except CompatibilityError as exc:
        print(f"pcbforge status: {exc}", file=sys.stderr)
        return 2
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

    if args.command != "init":
        try:
            validate_project_compatibility(Path(args.project_dir))
        except CompatibilityError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 2

    if args.command == "init":
        try:
            result = initialize_project(Path(args.project_dir))
        except InitInputError as exc:
            try:
                record_initialization_blocker(
                    Path(args.project_dir),
                    str(exc).splitlines()[0],
                )
            except StatusError:
                pass
            print(f"pcbforge init: {exc}", file=sys.stderr)
            return 2
        except InitError as exc:
            try:
                record_initialization_blocker(
                    Path(args.project_dir),
                    str(exc).splitlines()[0],
                )
            except StatusError:
                pass
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

    if args.command == "check-circuit-review":
        try:
            result = check_circuit_review(
                Path(args.project_dir),
                args.stage,
                write=args.write,
            )
        except CircuitReviewInputError as exc:
            print(f"pcbforge check-circuit-review: {exc}", file=sys.stderr)
            return 2
        except CircuitReviewError as exc:
            print(f"pcbforge check-circuit-review: {exc}", file=sys.stderr)
            return 1
        state = "wrote" if args.write and result.wrote else "validated"
        print(f"pcbforge: {state} {result.stage} circuit review evidence")
        print(f"pcbforge: {result.summary}")
        print(f"pcbforge: evidence fingerprint {result.fingerprint}")
        return 0

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

    if args.command in {"prepare-layout", "check-layout-handoff"}:
        try:
            result = (
                generate_brief(Path(args.project_dir))
                if args.command == "prepare-layout"
                else check_brief(Path(args.project_dir))
            )
        except PlacementInputError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 2
        except PlacementError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 1

        print(f"pcbforge: placement brief passed — {result.summary}")
        if args.command == "prepare-layout":
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
