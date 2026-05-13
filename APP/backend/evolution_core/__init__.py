"""
evolution_core — Darwin 进化框架共享核
公开 API: run_round, replay, ledger_tail
"""
from __future__ import annotations

from typing import Any


def run_round(
    module_id: str,
    max_rounds: int = 1,
    human_gate: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Run one or more mutation rounds for the given module.

    Args:
        module_id:   e.g. "classify", "reply", "reqpool"
        max_rounds:  number of mutation rounds to attempt
        human_gate:  if True, pause before applying mutations (CLI only)
        dry_run:     if True, propose mutations but do not apply

    Returns:
        dict with keys: rounds_attempted, rounds_kept, final_fitness
    """
    from APP.backend.evolution_core.cli import _get_adapter
    import argparse

    adapter = _get_adapter(module_id)

    # Build a minimal args namespace
    args = argparse.Namespace(max_rounds=max_rounds, dry_run=dry_run)

    from APP.backend.evolution_core.cli import cmd_run
    cmd_run(adapter, args)

    # Return ledger summary
    from APP.backend.evolution_core.ledger import Ledger
    ledger = Ledger(module_id)
    rows = ledger.tail(max_rounds)
    kept = sum(1 for r in rows if r.get("kept") == "1")
    last_fitness = float(rows[-1]["fitness_composite"]) if rows else 0.0

    return {
        "rounds_attempted": len(rows),
        "rounds_kept": kept,
        "final_fitness": last_fitness,
    }


def replay(
    module_id: str,
    genome_version: str,
    eval_set_id: str,
) -> dict[str, Any]:
    """
    Replay a genome version against an eval set.

    Args:
        module_id:      e.g. "classify"
        genome_version: hash string of the genome version to replay
        eval_set_id:    path or name of the eval set
                        ("rolling", "frozen", or full path to .jsonl/.csv)

    Returns:
        dict with keys: total, classified, fallback_rate
    """
    from APP.backend.evolution_core.cli import _get_adapter
    from pathlib import Path

    adapter = _get_adapter(module_id)

    # Resolve short names to full paths
    if eval_set_id in ("rolling", "frozen", "live_probe"):
        eval_set_path = str(
            Path.cwd() / "conclusion/_local/evolution" / module_id / "eval_sets" / f"{eval_set_id}.jsonl"
        )
    else:
        eval_set_path = eval_set_id

    inputs = adapter.build_replay_inputs(eval_set_path)
    outputs = adapter.run_pipeline(inputs)

    total = len(outputs)
    classified = sum(
        1 for o in outputs if o.get("classified_leaf") not in (None, "未分类", "")
    )
    fallback = total - classified

    return {
        "module_id": module_id,
        "genome_version": genome_version,
        "eval_set": eval_set_path,
        "total": total,
        "classified": classified,
        "fallback": fallback,
        "fallback_rate": fallback / total if total > 0 else 0.0,
    }


def ledger_tail(module_id: str, n: int = 20) -> list[dict[str, Any]]:
    """
    Return the last N ledger rows for the given module.
    """
    from APP.backend.evolution_core.ledger import Ledger
    return Ledger(module_id).tail(n)
