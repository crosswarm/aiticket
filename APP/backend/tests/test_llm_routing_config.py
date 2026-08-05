"""llm_feature_routing.json 的初始化守卫。

背景：这个文件此前【被 git 跟踪，却在运行时被设置页改写】。后果是
172 线上是全 deepseek、仓库版是 minimax/zhipu —— 内容长期分叉，
且一旦有人改了仓库版，172 一 pull 就会把线上路由悄悄换掉
（而 minimax 在那台机器上未必配了 key）。

改为：不跟踪，缺失时从代码里的默认值自动生成。
同目录的 llm_config.json / config/deployment.yaml 早就是这个模式，
它一直是异类。
"""
from __future__ import annotations

import json

import pytest

from services.llm_routing_config import (
    DEFAULT_ROUTING,
    ensure_routing_file,
    load_routing,
)


def test_creates_file_from_defaults_when_missing(tmp_path):
    p = tmp_path / "llm_feature_routing.json"
    assert not p.exists()

    created = ensure_routing_file(p)

    assert created is True
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == DEFAULT_ROUTING


def test_does_not_overwrite_existing_file(tmp_path):
    """★ 关键：已有文件是运维在设置页改出来的线上配置，绝不能被覆盖。"""
    p = tmp_path / "llm_feature_routing.json"
    live = {"_default": "deepseek", "smart_reply": "deepseek"}
    p.write_text(json.dumps(live), encoding="utf-8")

    created = ensure_routing_file(p)

    assert created is False
    assert json.loads(p.read_text(encoding="utf-8")) == live


def test_repairs_corrupted_file(tmp_path):
    """文件损坏时要能自愈，否则 8 个读取方会一起降级到兜底 provider。"""
    p = tmp_path / "llm_feature_routing.json"
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")

    ensure_routing_file(p)

    assert json.loads(p.read_text(encoding="utf-8")) == DEFAULT_ROUTING


def test_defaults_contain_no_provider_model_refs():
    """routing 文件只放裸 provider 名。含 ':' 会撑坏那些直接读该文件的后台消费方
    （identity_schema / reply_supervisor / weekly_report 等约 8 处）。"""
    for key, val in DEFAULT_ROUTING.items():
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            assert ":" not in v, f"{key} 的 provider 名不应含 ':'，实际 {v!r}"


def test_defaults_include_default_key():
    """_default 是所有未显式配置 feature 的兜底，必须存在。"""
    assert DEFAULT_ROUTING.get("_default")


def test_load_returns_defaults_when_file_absent(tmp_path):
    """读取方即使在 ensure 之前调用，也应拿到可用配置而不是空 dict。"""
    assert load_routing(tmp_path / "nope.json") == DEFAULT_ROUTING


def test_load_returns_defaults_when_file_corrupted(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("不是 JSON", encoding="utf-8")
    assert load_routing(p) == DEFAULT_ROUTING


def test_ensure_is_safe_to_call_repeatedly(tmp_path):
    p = tmp_path / "llm_feature_routing.json"
    assert ensure_routing_file(p) is True
    assert ensure_routing_file(p) is False
    assert ensure_routing_file(p) is False


def test_ensure_survives_unwritable_dir(tmp_path):
    """目录不可写时不能把启动流程带崩 —— 读取方本来就各有兜底。"""
    target = tmp_path / "nonexistent_parent" / "x" / "routing.json"
    # 不应抛异常
    ensure_routing_file(target)
