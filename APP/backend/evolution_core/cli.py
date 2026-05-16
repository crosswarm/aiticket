"""
Darwin Evolution Core CLI
Usage: python -m APP.backend.evolution_core <module> <command> [options]

Commands:
  classify status               — print current genome slots summary + recent ledger
  classify run [--max-rounds N] [--dry-run]  — run N mutation rounds
  classify replay --version <hash> --eval-set <path>  — replay genome version
  classify ledger [--tail N]    — show recent ledger entries
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _get_adapter(module_id: str):
    if module_id == "classify":
        from APP.backend.evolution_core.adapters.classify_adapter import ClassifyAdapter
        return ClassifyAdapter()
    if module_id == "reply":
        from APP.backend.evolution_core.adapters.reply_adapter import ReplyAdapter
        return ReplyAdapter()
    raise ValueError(f"Unknown module: '{module_id}'. Available: classify, reply")


def cmd_status(adapter, args) -> None:
    """Print current genome slots summary + recent ledger entries."""
    genome = adapter.read_genome()

    print(f"\n=== Genome: {genome.module_id} (v{genome.version}) ===")
    print(f"timestamp: {genome.timestamp}")
    if genome.parent_version:
        print(f"parent:    {genome.parent_version}")

    print("\n--- Slots ---")
    for slot_name, value in genome.slots.items():
        if value is None:
            print(f"  {slot_name}: [NOT FOUND]")
        elif isinstance(value, list):
            print(f"  {slot_name}: {len(value)} items")
            # Show first few
            for item in value[:3]:
                if isinstance(item, dict):
                    print(f"    - id={item.get('id', '?')}  keywords={len(item.get('keywords', []))} kws")
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    name, kws = item[0], item[1]
                    print(f"    - {name}: {len(kws)} keywords")
                else:
                    print(f"    - {str(item)[:80]}")
            if len(value) > 3:
                print(f"    ... ({len(value) - 3} more)")
        elif isinstance(value, str):
            short = value[:80].replace("\n", " ")
            print(f"  {slot_name}: {short!r}")
        else:
            print(f"  {slot_name}: {value!r}")

    # Recent ledger
    from APP.backend.evolution_core.ledger import Ledger
    ledger = Ledger(adapter.module_id)
    recent = ledger.tail(5)
    if recent:
        print(f"\n--- Recent Ledger ({len(recent)} entries) ---")
        for row in recent:
            kept = "KEPT" if row.get("kept") == "1" else "REVERTED"
            print(
                f"  [{row.get('round_id')}] {row.get('timestamp')}  "
                f"slot={row.get('slot_mutated')}  "
                f"fitness={row.get('fitness_composite')}  {kept}"
            )
    else:
        print("\n--- Ledger: (empty) ---")

    print()


def cmd_run(adapter, args) -> None:
    """Run N mutation rounds."""
    max_rounds: int = args.max_rounds
    dry_run: bool = args.dry_run
    use_fast: bool = getattr(args, "fast", False)
    write_report: bool = getattr(args, "report", False)

    from APP.backend.evolution_core.ledger import Ledger
    from APP.backend.evolution_core.ratchet import snapshot_genome, git_commit, git_revert
    from APP.backend.evolution_core.genome import genome_hash

    ledger = Ledger(adapter.module_id)
    report_lines: list[str] = []

    mode_label = "fast" if use_fast else "deep"
    print(f"\n=== Running {max_rounds} mutation round(s) [mode={mode_label}, dry_run={dry_run}] ===\n")
    report_lines.append(f"# Evolution Report: {adapter.module_id}")
    report_lines.append(f"- mode: {mode_label}")
    report_lines.append(f"- max_rounds: {max_rounds}")
    report_lines.append(f"- dry_run: {dry_run}")
    report_lines.append(f"- timestamp: {__import__('datetime').datetime.now().isoformat()}")
    report_lines.append("")

    for round_num in range(1, max_rounds + 1):
        round_id = f"round-{round_num:04d}"
        print(f"[{round_id}] Loading genome...")
        genome = adapter.read_genome()
        parent_version = genome.version

        print(f"[{round_id}] Scoring current genome ({mode_label})...")
        eval_set = str(
            Path.cwd() / "conclusion/_local/evolution" / adapter.module_id / "eval_sets/rolling.jsonl"
        )
        if use_fast and hasattr(adapter, "score_fast"):
            scores = adapter.score_fast(eval_set)
        else:
            scores = adapter.score(eval_set)
        print(f"[{round_id}] Scores: {scores}")
        report_lines.append(f"## {round_id}")
        report_lines.append(f"- parent: {parent_version}")
        report_lines.append(f"- scores: {scores}")

        # Find weakest dimension (exclude count/info fields)
        score_dims = {k: v for k, v in scores.items()
                      if k not in ("eval_count", "exact_match", "fallback_count",
                                   "total_generated", "total_adopted",
                                   "style_rules_chars", "cumulative_lessons")}
        if score_dims:
            weakest_dim = min(score_dims, key=lambda k: score_dims[k])
        else:
            weakest_dim = "fallback_rate"

        print(f"[{round_id}] Weakest dimension: {weakest_dim}")

        # Build misclassified context for propose_mutations
        misclassified = []
        if use_fast and hasattr(adapter, "run_pipeline"):
            inputs = adapter.build_replay_inputs(eval_set)
            if inputs:
                outputs = adapter.run_pipeline(inputs[:200])
                for inp, out in zip(inputs[:200], outputs):
                    pred = out.get("classified_leaf", "")
                    gold = inp.get("original_leaf", "")
                    if pred != gold and ("其他" in pred or pred == "未分类"):
                        misclassified.append(inp)

        # Propose mutations
        mutations = adapter.propose_mutations(
            weakest_dim,
            {"genome": genome, "audit_report": "", "recent_misclassified": misclassified},
        )

        if not mutations:
            print(f"[{round_id}] No mutations proposed. Stopping.")
            break

        print(f"[{round_id}] {len(mutations)} mutation(s) proposed:")
        for i, mut in enumerate(mutations[:3]):
            print(f"  [{i+1}] slot={mut['slot_name']}  rationale={mut['rationale'][:80]}")

        if dry_run:
            print(f"[{round_id}] DRY RUN — no changes applied.\n")
            continue

        # Apply first mutation
        mut = mutations[0]
        slot_name = mut["slot_name"]
        new_value = mut["new_value_slice"]

        print(f"[{round_id}] Applying mutation to slot '{slot_name}'...")
        from APP.backend.evolution_core.genome import write_genome
        write_genome(genome, slot_name, new_value)

        child_version = genome_hash(genome)
        genome.version = child_version
        genome.parent_version = parent_version

        if not dry_run:
            hash_val = snapshot_genome(genome)
            print(f"[{round_id}] Snapshot written: {hash_val}")

        # Re-score (same mode as initial score)
        if use_fast and hasattr(adapter, "score_fast"):
            new_scores = adapter.score_fast(eval_set)
        else:
            new_scores = adapter.score(eval_set)

        # Compute comparable fitness (exclude info/count fields)
        _EXCLUDE = {"eval_count", "exact_match", "fallback_count",
                     "total_generated", "total_adopted",
                     "style_rules_chars", "cumulative_lessons"}
        def _composite(s):
            dims = {k: v for k, v in s.items() if k not in _EXCLUDE}
            return sum(dims.values()) / len(dims) if dims else 0.0

        fitness_composite = _composite(new_scores)
        prev_composite = _composite(scores)

        kept = fitness_composite >= prev_composite
        revert_reason = "" if kept else "fitness_regression"

        print(f"[{round_id}] fitness: {prev_composite:.4f} → {fitness_composite:.4f}  kept={kept}")

        if not kept:
            print(f"[{round_id}] Reverting...")
            try:
                git_revert(adapter.module_id, round_id)
            except Exception as e:
                print(f"[{round_id}] git revert failed: {e}", file=sys.stderr)

        ledger.append(
            round_id=round_id,
            parent_version=parent_version,
            child_version=child_version,
            genome_hash=child_version,
            slot_mutated=slot_name,
            fitness_composite=fitness_composite,
            kept=kept,
            revert_reason=revert_reason,
            notes=mut.get("rationale", ""),
        )
        report_lines.append(f"- mutation: slot={slot_name}, kept={kept}")
        report_lines.append(f"- fitness: {prev_composite:.4f} → {fitness_composite:.4f}")
        report_lines.append("")
        print(f"[{round_id}] Ledger updated.\n")

    # Write daily report
    if write_report:
        from datetime import date
        rounds_dir = Path.cwd() / "conclusion/_local/evolution" / adapter.module_id / "rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        report_path = rounds_dir / f"report-{date.today().isoformat()}.md"
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        print(f"Report written: {report_path}")

    print("=== Run complete ===\n")


def cmd_replay(adapter, args) -> None:
    """Replay a genome version against an eval set."""
    version: str = args.version
    eval_set_path: str = args.eval_set

    root = Path.cwd()
    version_file = (
        root / "conclusion/_local/evolution" / adapter.module_id / "versions" / f"{version}.json"
    )

    if not version_file.exists():
        print(f"Version file not found: {version_file}", file=sys.stderr)
        sys.exit(1)

    with open(version_file, encoding="utf-8") as f:
        snapshot = json.load(f)

    print(f"\n=== Replay: {adapter.module_id} @ {version} ===")
    print(f"slots: {list(snapshot.get('slots', {}).keys())}")

    inputs = adapter.build_replay_inputs(eval_set_path)
    print(f"eval_set: {eval_set_path}  ({len(inputs)} tickets)")

    if not inputs:
        print("No inputs found in eval set.")
        return

    outputs = adapter.run_pipeline(inputs)

    # Summary
    classified = sum(1 for o in outputs if o.get("classified_leaf") not in (None, "未分类", ""))
    print(f"classified: {classified}/{len(outputs)}")

    fallback = len(outputs) - classified
    print(f"fallback (未分类): {fallback}/{len(outputs)} ({fallback/len(outputs)*100:.1f}%)")
    print()


def cmd_ledger(adapter, args) -> None:
    """Show recent ledger entries."""
    n: int = args.tail

    from APP.backend.evolution_core.ledger import Ledger
    ledger = Ledger(adapter.module_id)
    rows = ledger.tail(n)

    if not rows:
        print(f"Ledger empty for module '{adapter.module_id}'")
        return

    print(f"\n=== Ledger: {adapter.module_id} (last {len(rows)}) ===")
    header = f"{'round_id':<20} {'timestamp':<20} {'slot_mutated':<25} {'fitness':>8} {'kept':>5}"
    print(header)
    print("-" * len(header))
    for row in rows:
        kept = "Y" if row.get("kept") == "1" else "N"
        print(
            f"{row.get('round_id', ''):<20} "
            f"{row.get('timestamp', ''):<20} "
            f"{row.get('slot_mutated', ''):<25} "
            f"{row.get('fitness_composite', ''):>8} "
            f"{kept:>5}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="evolution_core",
        description="Darwin 进化框架 CLI",
    )
    parser.add_argument("module", help="module id (e.g. classify)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Print genome slots summary + recent ledger")

    # run
    run_parser = subparsers.add_parser("run", help="Run mutation rounds")
    run_parser.add_argument("--max-rounds", type=int, default=1, dest="max_rounds")
    run_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    run_parser.add_argument("--fast", action="store_true", help="Use fast score (keyword accuracy, no LLM)")
    run_parser.add_argument("--report", action="store_true", help="Write daily report to rounds/ dir")

    # replay
    replay_parser = subparsers.add_parser("replay", help="Replay a genome version")
    replay_parser.add_argument("--version", required=True)
    replay_parser.add_argument("--eval-set", required=True, dest="eval_set")

    # ledger
    ledger_parser = subparsers.add_parser("ledger", help="Show recent ledger entries")
    ledger_parser.add_argument("--tail", type=int, default=20)

    args = parser.parse_args()

    adapter = _get_adapter(args.module)

    if args.command == "status":
        cmd_status(adapter, args)
    elif args.command == "run":
        cmd_run(adapter, args)
    elif args.command == "replay":
        cmd_replay(adapter, args)
    elif args.command == "ledger":
        cmd_ledger(adapter, args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
