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
from pcbforge.kicad_sch import RenderResult, ReviewSchematic, SchematicError, export_preview, probe_text
from pcbforge.fab import (
    FabError,
    FabInputError,
    check_fab,
    generate_fab,
    render_fab_result,
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
from pcbforge.placement_check import (
    PlacementCheckError,
    PlacementCheckInputError,
    check_placement,
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
    finish_architect,
    inspect_status,
    mark_policy,
    mark_status,
    policy_approval_context,
    prepare_cascade_review,
    read_status_document,
    record_initialization_blocker,
    render_cascade_review,
    render_next,
    render_phase_review,
    render_terminal,
    review_phase,
    run_status_checks,
    renew_cascade,
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

    finish_architect_parser = subcommands.add_parser(
        "finish-architect",
        help="record the checked ARCHITECT to CIRCUIT source baseline",
        description=(
            "Require the approved ARCHITECT proposal, current build and IOC "
            "checks, and an unchanged spatial board, then capture the CIRCUIT "
            "source baseline."
        ),
    )
    finish_architect_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
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
            "Validate the exact proposal model and the generated review "
            "schematic (structure, ERC, pin-exact netlist parity). Final checks "
            "compare the approved model directly with the compiled Atopile BOM "
            "and PCB topology."
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

    render_circuit_parser = subcommands.add_parser(
        "render-circuit",
        help="run the authored circuit schematic script",
        description=(
            "Execute review/circuit/circuit_schematic.py inside the pinned "
            "toolchain. The script places symbols and wires through "
            "pcbforge.kicad_sch.ReviewSchematic, whose save step embeds KiCad 9 "
            "symbols, stamps the model fingerprint, generates group boxes and "
            "registers, lints readability, runs ERC, and proves the netlist "
            "against the model with the same gate as check-circuit-review."
        ),
    )
    render_circuit_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )
    render_circuit_parser.add_argument(
        "--svg",
        action="store_true",
        help="also export review/circuit/preview/circuit.svg (and .png when a rasterizer exists)",
    )
    render_circuit_parser.add_argument(
        "--probe",
        metavar="REFS",
        help=(
            "do not render; print the resolved symbol, pins, model nets and pin-tip "
            "offsets per rotation for REFS (comma separated) or 'all'"
        ),
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

    check_placement_parser = subcommands.add_parser(
        "check-placement",
        help="measure the board against placement.yaml (advisory)",
        description=(
            "Measure the current board against the placement contract and "
            "report every constraint, courtyard overlap, and outline result "
            "with its distance. Advisory: it never changes the board and "
            "never gates a phase."
        ),
    )
    check_placement_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )
    check_placement_parser.add_argument(
        "--write-report",
        action="store_true",
        help="atomically write docs/placement-check.md",
    )
    check_placement_parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print findings that need a human judgement",
    )

    fab_out_parser = subcommands.add_parser(
        "fab-out",
        help="generate and validate the VERIFY-to-ORDER fabrication packet",
        description=(
            "Plot Gerbers, drills, placement, and DRC evidence from the "
            "approved board, derive the JLC BOM and CPL, archive the packet, "
            "and record the checked FAB-OUT transition. Never changes the PCB."
        ),
    )
    fab_out_parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="initialized pcbforge project (default: current directory)",
    )
    check_fab_parser = subcommands.add_parser(
        "check-fab-out",
        help="validate the recorded fabrication packet without writing",
    )
    check_fab_parser.add_argument(
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
        "--force-checks",
        action="store_true",
        help="rerun all applicable checks instead of reusing current passes",
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
        help="blocked, reopened, skipped, or ai-assisted",
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
            "Build the exact artifact/check packet for user approval and save "
            "its ready fingerprint in STATUS.md."
        ),
    )
    parser.add_argument(
        "phase",
        nargs="?",
        help="workflow phase key, such as architect or circuit",
    )
    parser.add_argument(
        "--cascade",
        action="store_true",
        help="review a stale approval chain for consolidated renewal",
    )
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


def _status_renew_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbforge status renew",
        description=(
            "Record one explicit user decision across the unchanged prefix "
            "printed by `pcbforge status review --cascade`."
        ),
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        metavar="PROJECT_DIR",
        help="project containing spec.md (default: current directory)",
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--fingerprint",
        help="exact SHA-256 printed by the cascade review command",
    )
    selector.add_argument(
        "--last-reviewed",
        action="store_true",
        help="use the latest ready cascade review saved in STATUS.md",
    )
    parser.add_argument(
        "--note",
        required=True,
        help="the explicit user approval recorded across renewed gate history",
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
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--fingerprint",
        help="exact SHA-256 printed by the phase review command",
    )
    selector.add_argument(
        "--last-reviewed",
        action="store_true",
        help="use the latest ready matching review saved in STATUS.md",
    )
    parser.add_argument(
        "--note",
        required=True,
        help="the explicit user approval recorded in append-only history",
    )
    return parser


def _run_status_cli(argv: list[str]) -> int:
    mode = (
        argv[0]
        if argv and argv[0] in {"mark", "review", "approve", "renew"}
        else "show"
    )
    parser = {
        "mark": _status_mark_parser,
        "review": _status_review_parser,
        "approve": _status_approve_parser,
        "renew": _status_renew_parser,
        "show": _status_show_parser,
    }[mode]()
    status_args = parser.parse_args(argv[1:] if mode != "show" else argv)
    try:
        status_project_dir = status_args.project_dir
        if mode == "review" and status_args.cascade:
            if status_args.stage != "final":
                raise StatusInputError(
                    "cascade review cannot be combined with --stage"
                )
            if status_args.project_dir != "." and status_args.phase is not None:
                raise StatusInputError(
                    "cascade review accepts one optional project directory"
                )
            if status_args.phase is not None:
                status_project_dir = status_args.phase
        validate_project_compatibility(Path(status_project_dir))
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
            if status_args.cascade:
                cascade = prepare_cascade_review(
                    Path(status_project_dir),
                    record=True,
                )
                print(render_cascade_review(cascade))
                return 0 if cascade.ready else 1
            if status_args.phase is None:
                raise StatusInputError(
                    "phase is required unless --cascade is used"
                )
            review = review_phase(
                Path(status_args.project_dir),
                status_args.phase,
                stage=status_args.stage,
            )
            print(render_phase_review(review))
            return 0 if review.ready else 1
        if mode == "renew":
            result = renew_cascade(
                Path(status_args.project_dir),
                status_args.fingerprint,
                status_args.note,
                last_reviewed=status_args.last_reviewed,
            )
            print(render_terminal(result.report))
            print(
                "pcbforge: recorded explicit cascade renewal; "
                f"{'updated' if result.wrote else 'unchanged'} STATUS.md"
            )
            return 0
        if mode == "approve":
            result = approve_phase(
                Path(status_args.project_dir),
                status_args.phase,
                status_args.fingerprint,
                status_args.note,
                stage=status_args.stage,
                last_reviewed=status_args.last_reviewed,
            )
            print(render_terminal(result.report))
            print(
                f"pcbforge: recorded explicit {status_args.phase} approval; "
                f"{'updated' if result.wrote else 'unchanged'} STATUS.md"
            )
            return 0

        if status_args.force_checks and not status_args.check:
            raise StatusInputError("--force-checks requires --check")
        if status_args.write:
            result = write_status(
                Path(status_args.project_dir),
                check=status_args.check,
                force_checks=status_args.force_checks,
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
                document = run_status_checks(
                    project_dir,
                    document,
                    force_checks=status_args.force_checks,
                )
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

    if args.command == "finish-architect":
        try:
            result = finish_architect(Path(args.project_dir))
        except StatusInputError as exc:
            print(f"pcbforge finish-architect: {exc}", file=sys.stderr)
            return 2
        except StatusCheckError as exc:
            print(f"pcbforge finish-architect: {exc}", file=sys.stderr)
            return 1
        except StatusError as exc:
            print(f"pcbforge finish-architect: {exc}", file=sys.stderr)
            return 1
        print(render_terminal(result.report))
        print("pcbforge: recorded ARCHITECT → CIRCUIT architecture baseline")
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
        for warning in result.diagram_warnings:
            print(f"pcbforge: schematic warning {warning}")
        print(f"pcbforge: evidence fingerprint {result.fingerprint}")
        return 0

    if args.command == "render-circuit":
        import runpy

        project_dir = Path(args.project_dir).expanduser().resolve()
        if args.probe:
            try:
                sch = ReviewSchematic(project_dir, title="probe", desc="probe")
                refs = None if args.probe.strip() == "all" else [r.strip() for r in args.probe.split(",") if r.strip()]
                print(probe_text(sch, refs))
            except (SchematicError, CircuitReviewError) as exc:
                print(f"pcbforge render-circuit: {exc}", file=sys.stderr)
                return 2
            return 0
        script = project_dir / "review" / "circuit" / "circuit_schematic.py"
        if not script.is_file():
            print(
                "pcbforge render-circuit: missing review/circuit/"
                "circuit_schematic.py — author it per agent/circuit-kicad.md",
                file=sys.stderr,
            )
            return 2
        try:
            namespace = runpy.run_path(str(script), run_name="__main__")
        except (SchematicError, CircuitReviewError) as exc:
            import traceback

            where = ""
            for frame in reversed(traceback.extract_tb(exc.__traceback__)):
                if Path(frame.filename).name == script.name:
                    where = f" (at {script.name}:{frame.lineno}: {frame.line})"
                    break
            print(f"pcbforge render-circuit: {exc}{where}", file=sys.stderr)
            return 1
        result = namespace.get("result")
        if isinstance(result, RenderResult):
            for reference, choice in result.symbol_choices.items():
                print(f"pcbforge: symbol {reference} -> {choice.symbol.lib_id} ({choice.reason})")
            for warning in result.warnings:
                print(f"pcbforge: schematic warning [{warning.code}] {warning.message}")
            print(f"pcbforge: {result.summary}")
        if args.svg:
            try:
                outputs = export_preview(project_dir)
            except (SchematicError, CircuitReviewInputError) as exc:
                print(f"pcbforge render-circuit: {exc}", file=sys.stderr)
                return 1
            for path in outputs:
                print(f"pcbforge: preview {path.relative_to(project_dir).as_posix()}")
        print("pcbforge: rendered and validated the circuit review schematic")
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

    if args.command == "check-placement":
        try:
            result = check_placement(
                Path(args.project_dir),
                write_report=args.write_report,
            )
        except PlacementCheckInputError as exc:
            print(f"pcbforge check-placement: {exc}", file=sys.stderr)
            return 2
        except PlacementCheckError as exc:
            print(f"pcbforge check-placement: {exc}", file=sys.stderr)
            return 1

        print(f"pcbforge: placement check — {result.summary}")
        for finding in result.findings:
            if finding.status == "fail" or (
                args.verbose and finding.status in {"manual", "unmeasured"}
            ):
                detail = f"  ({finding.detail})" if finding.detail else ""
                limit = f"  limit {finding.limit}" if finding.limit else ""
                print(
                    f"  {finding.status.upper():10} {finding.identifier}  "
                    f"measured {finding.measured}{limit}{detail}"
                )
        for warning in result.warnings:
            print(f"  WARNING    {warning}")
        if args.write_report:
            state = "updated" if result.wrote_report else "unchanged"
            print(f"pcbforge: {state} {result.report_path.as_posix()}; PCB unchanged")
        return 1 if result.failures else 0

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
        for warning in result.warnings:
            print(f"  WARNING    {warning}")
        if args.command == "prepare-layout":
            brief_state = "updated" if result.wrote_brief else "unchanged"
            project_state = "updated" if result.wrote_project else "unchanged"
            print(
                f"pcbforge: {brief_state} {result.brief_path.as_posix()}; "
                f"{project_state} {result.project_path.as_posix()}; PCB unchanged"
            )
        return 0

    if args.command in {"fab-out", "check-fab-out"}:
        try:
            result = (
                generate_fab(Path(args.project_dir))
                if args.command == "fab-out"
                else check_fab(Path(args.project_dir))
            )
        except FabInputError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 2
        except FabError as exc:
            print(f"pcbforge {args.command}: {exc}", file=sys.stderr)
            return 1

        print(render_fab_result(result))
        if args.command == "fab-out":
            print(
                "pcbforge: recorded the VERIFY → ORDER FAB-OUT transition"
                if result.recorded
                else "pcbforge: packet regenerated; FAB-OUT transition already current"
            )
            print("pcbforge: PCB unchanged; ordering remains a human decision")
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
