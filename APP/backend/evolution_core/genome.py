"""
Genome：Darwin 进化框架的基因数据结构
每个 module 的 genome 由若干 slot 组成，每个 slot 指向一个源文件的具体值。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Genome:
    module_id: str
    version: str          # SHA256 hash of all slot values
    slots: dict[str, Any]
    parent_version: str | None = None
    timestamp: float = field(default_factory=time.time)


def _extract_python_list(source: str, varname: str) -> list[Any] | None:
    """
    从 Python 源码提取顶层变量赋值的列表字面量。
    支持 R_SPECIFIC_RULES = [...] 形式（元组列表）。
    """
    # 匹配变量名后的 = [...] 块
    pattern = re.compile(
        rf"^{re.escape(varname)}\s*=\s*(\[)",
        re.MULTILINE,
    )
    m = pattern.search(source)
    if not m:
        return None
    start = m.start(1)
    depth = 0
    for i, ch in enumerate(source[start:]):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                snippet = source[start : start + i + 1]
                try:
                    return eval(snippet)  # noqa: S307 — trusted local files only
                except Exception:
                    return None
    return None


def _extract_python_regex(source: str, varname: str) -> str | None:
    """从 Python 源码提取 re.compile(r'...') 的原始 pattern 字符串。"""
    m = re.search(rf"^{re.escape(varname)}\s*=\s*re\.compile\(", source, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    depth = 1
    for i, ch in enumerate(source[start:]):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                block = source[start:start + i]
                parts = re.findall(r"r['\"]([^'\"]*)['\"]", block)
                if parts:
                    return "|".join(parts)
                return None
    return None


def _dotpath_get(data: Any, dotpath: str) -> Any:
    """
    点路径导航：支持 'agents.reply_generator.params.similar_issues_top_k' 形式。
    对 dict 递归取键，遇到 list 时尝试按整数索引。
    """
    parts = dotpath.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _extract_python_scalar(source: str, varname: str) -> Any:
    """
    从 Python 源码提取顶层标量常量赋值，例如：
      SEARCH_EXAMPLES_MODIFIED_BOOST = 1.3
    支持 int / float / str。
    """
    pattern = re.compile(
        rf"^{re.escape(varname)}\s*=\s*([^\n#]+)",
        re.MULTILINE,
    )
    m = pattern.search(source)
    if not m:
        return None
    raw = m.group(1).strip()
    # Remove inline comments
    raw = re.sub(r"\s*#.*$", "", raw).strip()
    try:
        return eval(raw)  # noqa: S307 — trusted local files only
    except Exception:
        return None


def _read_slot_value(slot_def: dict, project_root: Path) -> Any:
    """根据 slot 定义读取当前值"""
    kind = slot_def.get("kind", "rule_list")
    path_str = slot_def.get("path", "")
    key = slot_def.get("key", "")

    abs_path = project_root / path_str

    if not abs_path.exists():
        return slot_def.get("default")

    source = abs_path.read_text(encoding="utf-8")

    # markdown_section: whole file IS the slot value
    if kind == "markdown_section":
        return source

    if abs_path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(source)
        if key:
            # Support dot-path navigation (e.g. agents.reply_generator.params.top_k)
            if "." in key:
                return _dotpath_get(data, key)
            return data.get(key)
        return data

    # Python source file
    if kind == "rule_list":
        return _extract_python_list(source, key)
    elif kind == "regex":
        return _extract_python_regex(source, key)
    elif kind == "numeric":
        # key is a top-level Python constant name
        return _extract_python_scalar(source, key)

    return None


def load_genome(registry_path: str) -> Genome:
    """
    读取 YAML registry 文件，解析每个 slot 为当前值。
    registry_path 相对于 Path.cwd()。
    """
    root = Path.cwd()
    reg_abs = root / registry_path

    with open(reg_abs, encoding="utf-8") as f:
        reg = yaml.safe_load(f)

    module_id = reg["module_id"]
    raw_slots = reg.get("slots", {})

    resolved: dict[str, Any] = {}
    for slot_name, slot_def in raw_slots.items():
        value = _read_slot_value(slot_def, root)
        resolved[slot_name] = value

    version = genome_hash_from_slots(resolved)

    return Genome(
        module_id=module_id,
        version=version,
        slots=resolved,
    )


def genome_hash(genome: Genome) -> str:
    return genome_hash_from_slots(genome.slots)


def genome_hash_from_slots(slots: dict[str, Any]) -> str:
    """SHA256 of all slot values concatenated (JSON-serialized, sorted keys)."""
    payload = json.dumps(slots, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def write_genome(genome: Genome, slot_name: str, new_value: Any) -> None:
    """
    Write a single slot back to its source file.
    Currently supports simple reassignment for known slot types.
    Uses ratchet.apply_to_files for the actual file write.
    """
    from APP.backend.evolution_core.ratchet import apply_to_files
    apply_to_files(genome, slot_name, new_value)
