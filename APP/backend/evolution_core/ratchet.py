"""
Ratchet：基因快照 + git commit/revert
- shadow 快照：conclusion/_local/evolution/<module>/versions/<hash>.json
- git 集成：evo/<module>/round-<N> 分支上 commit / revert
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from APP.backend.evolution_core.constants import EVOLUTION_DIR

if TYPE_CHECKING:
    from APP.backend.evolution_core.genome import Genome


def snapshot_genome(genome: "Genome") -> str:
    """
    Serialize all genome slots to JSON and write to
    conclusion/_local/evolution/<module>/versions/<hash>.json
    Returns the hash (same as genome.version).
    """
    root = Path.cwd()
    versions_dir = root / EVOLUTION_DIR / genome.module_id / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "module_id": genome.module_id,
        "version": genome.version,
        "parent_version": genome.parent_version,
        "timestamp": genome.timestamp,
        "slots": genome.slots,
    }
    out_path = versions_dir / f"{genome.version}.json"
    out_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return genome.version


def apply_to_files(genome: "Genome", slot_name: str, new_value: Any) -> None:
    """
    Write a single slot's new value back to its source file.
    Reads registry to find the file path and slot kind.
    """
    from APP.backend.evolution_core.genome import _read_slot_value

    root = Path.cwd()
    registry_path = root / "APP/backend/evolution_core/registry" / f"{genome.module_id}.yaml"

    if not registry_path.exists():
        raise FileNotFoundError(f"Registry not found: {registry_path}")

    with open(registry_path, encoding="utf-8") as f:
        reg = yaml.safe_load(f)

    slot_def = reg.get("slots", {}).get(slot_name)
    if slot_def is None:
        raise ValueError(f"Slot '{slot_name}' not found in registry for {genome.module_id}")

    kind = slot_def.get("kind", "rule_list")
    path_str = slot_def.get("path", "")
    key = slot_def.get("key", "")
    abs_path = root / path_str

    if not abs_path.exists():
        raise FileNotFoundError(f"Target file not found: {abs_path}")

    if kind == "markdown_section":
        abs_path.write_text(new_value, encoding="utf-8")
    elif abs_path.suffix in (".yaml", ".yml"):
        with open(abs_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if key:
            data[key] = new_value
        else:
            data = new_value
        with open(abs_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
    else:
        # Python source — rewrite the named variable
        source = abs_path.read_text(encoding="utf-8")
        new_source = _rewrite_python_variable(source, key, new_value)
        abs_path.write_text(new_source, encoding="utf-8")

    # Update in-memory genome
    genome.slots[slot_name] = new_value


def _rewrite_python_variable(source: str, varname: str, new_value: Any) -> str:
    """
    Replace the RHS of `varname = <expr>` with repr(new_value).
    Simple single-pass replacement; handles list literals only.
    """
    import re

    pattern = re.compile(
        rf"^({re.escape(varname)}\s*=\s*)(\[.*?\])",
        re.DOTALL | re.MULTILINE,
    )
    replacement = rf"\g<1>{repr(new_value)}"
    new_source, count = pattern.subn(replacement, source, count=1)
    if count == 0:
        raise ValueError(f"Could not find variable '{varname}' in source")
    return new_source


def git_commit(module_id: str, round_id: str, message: str) -> None:
    """
    git add all changed files tracked by the repo, then commit.
    """
    root = Path.cwd()
    subprocess.run(
        ["git", "add", "-u"],
        cwd=str(root),
        check=True,
    )
    full_msg = f"[evo/{module_id}/{round_id}] {message}"
    subprocess.run(
        ["git", "commit", "-m", full_msg],
        cwd=str(root),
        check=True,
    )


def git_revert(module_id: str, round_id: str) -> None:
    """
    Revert the last commit if its message contains the round_id.
    Uses `git revert HEAD --no-edit`.
    """
    root = Path.cwd()
    # Verify last commit belongs to this round
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    last_msg = result.stdout.strip()
    if round_id not in last_msg:
        raise ValueError(
            f"Last commit '{last_msg}' does not match round_id '{round_id}'. "
            "Aborting revert to avoid corrupting history."
        )
    subprocess.run(
        ["git", "revert", "HEAD", "--no-edit"],
        cwd=str(root),
        check=True,
    )
