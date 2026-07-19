"""用户级 LLM 配置改造测试。

覆盖：
  A. auth_service CRUD —— set/get/delete user_llm_config + last_provider（加密往返、隔离 DB）。
  B. resolve_feature_llm_runtime 四路径：
     1) 用户配了该 provider           → _source=user
     2) 用户没配 + 该功能允许系统兜底  → _source=system
     3) 用户没配 + 该功能禁止系统兜底  → _blocked
     4) 后台 user_id=None：
        - 允许兜底 → _source=system
        - 禁止兜底 → _blocked

环境：
  - auth_service CRUD 用临时 sqlite（构造 AuthService(db_path=tmp)）隔离。
  - resolve_* 用 monkeypatch 替换 main.auth_service / main.load_llm_config，
    不触碰真实 DB / 文件。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ─────────────────────────── A. auth_service CRUD ───────────────────────────

def _make_service(tmp_path):
    from auth_service import AuthService
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )
    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    member = service.create_user(
        "alice", "member-pass", display_name="Alice", role="member", created_by=admin["id"]
    )
    return service, member["id"]


def test_set_get_user_llm_config_roundtrip(tmp_path):
    service, uid = _make_service(tmp_path)

    # 初始为空
    assert service.get_user_llm_config(uid) == {}

    service.set_user_llm_provider(
        uid, "zhipu", api_key="sk-user-zhipu", model_name="glm-5", base_url="https://z.example/v1"
    )
    cfg = service.get_user_llm_config(uid)
    assert "zhipu" in cfg
    assert cfg["zhipu"]["api_key"] == "sk-user-zhipu"  # 解密往返
    assert cfg["zhipu"]["model_name"] == "glm-5"
    assert cfg["zhipu"]["base_url"] == "https://z.example/v1"


def test_set_user_llm_config_is_encrypted_at_rest(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "openai", api_key="sk-secret-123")

    # 直查 DB：api_key 列不得为明文
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "auth.db"))
    row = conn.execute(
        "SELECT api_key FROM user_llm_config WHERE user_id=? AND provider=?", (uid, "openai")
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] != "sk-secret-123"
    assert row[0]  # 非空（已加密）


def test_upsert_overwrites_same_provider(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "zhipu", api_key="k1", model_name="m1")
    service.set_user_llm_provider(uid, "zhipu", api_key="k2", model_name="m2")
    cfg = service.get_user_llm_config(uid)
    assert cfg["zhipu"]["api_key"] == "k2"
    assert cfg["zhipu"]["model_name"] == "m2"


def test_delete_user_llm_provider(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "zhipu", api_key="k1")
    service.set_user_llm_provider(uid, "openai", api_key="k2")
    service.delete_user_llm_provider(uid, "zhipu")
    cfg = service.get_user_llm_config(uid)
    assert "zhipu" not in cfg
    assert "openai" in cfg


def test_user_last_provider_roundtrip(tmp_path):
    service, uid = _make_service(tmp_path)
    assert service.get_user_last_provider(uid) == ""
    service.set_user_last_provider(uid, "minimax")
    assert service.get_user_last_provider(uid) == "minimax"
    service.set_user_last_provider(uid, "zhipu")
    assert service.get_user_last_provider(uid) == "zhipu"


def test_get_user_llm_config_empty_user_id(tmp_path):
    service, _ = _make_service(tmp_path)
    assert service.get_user_llm_config("") == {}
    assert service.get_user_last_provider("") == ""


# ──────────────────── B. resolve_feature_llm_runtime 四路径 ────────────────────

class _FakeAuthService:
    """最小桩：只实现 resolve_feature_llm_runtime 用到的两个方法。"""

    def __init__(self, routing, user_cfg, fallback_override=None, model_map=None):
        self._routing = routing
        self._user_cfg = user_cfg
        self._fallback_override = fallback_override or {}
        self._model_map = model_map or {}

    def get_system_setting(self, key, default=None):
        import main
        if key == main.LLM_FEATURE_ROUTING_KEY:
            return self._routing
        if key == main.LLM_FEATURE_FALLBACK_KEY:
            return self._fallback_override
        if key == main.LLM_FEATURE_MODEL_KEY:
            return self._model_map
        return default

    def get_user_llm_config(self, user_id):
        if not user_id:
            return {}
        return self._user_cfg.get(user_id, {})

    def get_user_last_provider(self, user_id):
        return self._last_provider.get(user_id, "") if hasattr(self, "_last_provider") else ""


@pytest.fixture
def main_mod():
    import main
    return main


def _patch(main, monkeypatch, *, routing, user_cfg, system_cfg, fallback_override=None, model_map=None):
    fake = _FakeAuthService(routing, user_cfg, fallback_override, model_map)
    monkeypatch.setattr(main, "auth_service", fake)
    monkeypatch.setattr(main, "load_llm_config", lambda: system_cfg)


def test_path1_user_configured_returns_user(main_mod, monkeypatch):
    # smart_reply 默认禁止系统兜底；用户配了 zhipu → 走用户级
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={"u1": {"zhipu": {"api_key": "sk-user", "model_name": "glm-5", "base_url": "https://u"}}},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "user"
    assert rt["api_key"] == "sk-user"
    assert rt["provider"] == "zhipu"
    assert not rt.get("_blocked")


def test_path2_no_user_fallback_on_returns_system(main_mod, monkeypatch):
    # darwin_eval 默认允许系统兜底；用户没配 → 走系统级
    _patch(
        main_mod, monkeypatch,
        routing={"darwin_eval": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system", "model_name": "glm-5"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("darwin_eval", user_id="u1")
    assert rt["_source"] == "system"
    assert rt["api_key"] == "sk-system"
    assert not rt.get("_blocked")


def test_path3_no_user_fallback_off_blocks(main_mod, monkeypatch):
    # smart_reply 默认禁止系统兜底；用户没配 → 阻断
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt.get("_blocked") is True
    assert rt["_source"] == "blocked"
    assert rt["api_key"] == ""
    assert rt["_reason"] == "feature_requires_user_llm"


def test_path4_background_no_user_fallback_on_returns_system(main_mod, monkeypatch):
    # 后台 user_id=None + 允许兜底 → 系统级
    _patch(
        main_mod, monkeypatch,
        routing={"weekly_report": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("weekly_report", user_id=None)
    assert rt["_source"] == "system"
    assert rt["api_key"] == "sk-system"


def test_path4_background_no_user_fallback_off_blocks(main_mod, monkeypatch):
    # 后台 user_id=None + 禁止兜底（smart_reply 默认 False）→ 阻断
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id=None)
    assert rt.get("_blocked") is True
    assert rt["_source"] == "blocked"


def test_admin_override_can_enable_fallback(main_mod, monkeypatch):
    # admin 把 smart_reply 兜底打开 → 用户没配也能走系统级
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
        fallback_override={"smart_reply": True},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "system"
    assert rt["api_key"] == "sk-system"


def test_unknown_feature_defaults_to_fallback_true(main_mod, monkeypatch):
    # 未知 feature 缺省允许兜底
    _patch(
        main_mod, monkeypatch,
        routing={"_default": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("some_new_feature", user_id=None)
    assert rt["_source"] == "system"


def test_exclude_providers_respected(main_mod, monkeypatch):
    # 降级链 [zhipu, openai]，排除 zhipu → 用 openai 的用户凭据
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": ["zhipu", "openai"]},
        user_cfg={"u1": {"openai": {"api_key": "sk-openai-user"}}},
        system_cfg={},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1", exclude_providers=["zhipu"])
    assert rt["_source"] == "user"
    assert rt["provider"] == "openai"
    assert rt["api_key"] == "sk-openai-user"


# ──────────── C. bugfix: 用户 provider 不在路由链时仍应命中用户级 ────────────
# 生产案例：songshijia 配了 deepseek，但 smart_reply 路由 minimax →
# 修复前用户级只查路由链候选 → 误判 blocked。

def test_user_provider_outside_routing_chain_still_used(main_mod, monkeypatch):
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "minimax"},  # 路由链里没有 deepseek
        user_cfg={"u1": {"deepseek": {"api_key": "sk-ds-user", "model_name": "deepseek-v4-flash", "base_url": "https://ds"}}},
        system_cfg={"minimax": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "user"
    assert rt["provider"] == "deepseek"
    assert rt["api_key"] == "sk-ds-user"
    assert not rt.get("_blocked")


def test_user_last_provider_preferred_in_fallback_order(main_mod, monkeypatch):
    fake = _FakeAuthService(
        {"smart_reply": "minimax"},
        {"u1": {
            "deepseek": {"api_key": "sk-ds", "model_name": "", "base_url": ""},
            "openai": {"api_key": "sk-oa", "model_name": "", "base_url": ""},
        }},
    )
    fake._last_provider = {"u1": "openai"}
    monkeypatch.setattr(main_mod, "auth_service", fake)
    monkeypatch.setattr(main_mod, "load_llm_config", lambda: {})
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "user"
    assert rt["provider"] == "openai"  # last_provider 优先


def test_exclude_providers_applies_to_user_fallback(main_mod, monkeypatch):
    # 用户只配 deepseek 但被 exclude（429 failover 场景）→ 用户级不命中 → blocked
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "minimax"},
        user_cfg={"u1": {"deepseek": {"api_key": "sk-ds", "model_name": "", "base_url": ""}}},
        system_cfg={},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1", exclude_providers=["deepseek"])
    assert rt.get("_blocked") is True


# ─────────────── D. 源→多 model：端点 ref 解析 + 归一化 helper ───────────────

def test_parse_endpoint_ref(main_mod):
    assert main_mod._parse_endpoint_ref("deepseek") == ("deepseek", None)
    assert main_mod._parse_endpoint_ref("dashscope:glm-5") == ("dashscope", "glm-5")
    assert main_mod._parse_endpoint_ref("  dashscope : glm-5 ") == ("dashscope", "glm-5")
    assert main_mod._parse_endpoint_ref("") == (None, None)
    assert main_mod._parse_endpoint_ref(None) == (None, None)


def test_source_models_helper(main_mod):
    # 旧条目无 models → 退化 [model_name]
    assert main_mod._source_models({"model_name": "glm-5"}) == ["glm-5"]
    # 有 models 且默认不在其中 → 默认排最前
    assert main_mod._source_models({"model_name": "glm-5", "models": ["qwen-max"]}) == ["glm-5", "qwen-max"]
    # 默认已在 models → 保序去重，默认在前
    assert main_mod._source_models({"model_name": "glm-5", "models": ["glm-5", "qwen-max", "glm-5"]}) == ["glm-5", "qwen-max"]
    # 无 model_name 无 models → 空
    assert main_mod._source_models({}) == []


# ─────────────── E. 源→多 model：resolver 按 ref 选 model ───────────────

def test_endpoint_ref_selects_specific_model_user(main_mod, monkeypatch):
    # 用户配了 dashscope(默认 glm-5)，路由指定 dashscope:qwen-max → 用 qwen-max + 用户 key
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "dashscope:qwen-max"},
        user_cfg={"u1": {"dashscope": {"api_key": "sk-user", "model_name": "glm-5", "base_url": "https://d"}}},
        system_cfg={},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "user"
    assert rt["provider"] == "dashscope"
    assert rt["model_name"] == "qwen-max"   # ref 指定优先于源默认
    assert rt["api_key"] == "sk-user"


def test_bare_source_ref_uses_default_model_system(main_mod, monkeypatch):
    # 裸源 ref → 用该源默认 model（系统兜底路径）
    _patch(
        main_mod, monkeypatch,
        routing={"darwin_eval": "dashscope"},
        user_cfg={},
        system_cfg={"dashscope": {"api_key": "sk-sys", "model_name": "glm-5", "models": ["glm-5", "qwen-max"]}},
    )
    rt = main_mod.resolve_feature_llm_runtime("darwin_eval", user_id=None)
    assert rt["_source"] == "system"
    assert rt["model_name"] == "glm-5"


def test_two_features_same_source_different_models_user(main_mod, monkeypatch):
    # 同一个源(用户一把 key)，两功能路由到不同 model → 各自命中，均用用户 key
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "dashscope:glm-5", "classification": "dashscope:qwen-max"},
        user_cfg={"u1": {"dashscope": {"api_key": "sk-user", "model_name": "glm-5", "base_url": "https://d"}}},
        system_cfg={},
    )
    r1 = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    r2 = main_mod.resolve_feature_llm_runtime("classification", user_id="u1")
    assert r1["model_name"] == "glm-5" and r1["_source"] == "user"
    assert r2["model_name"] == "qwen-max" and r2["_source"] == "user"
    assert r1["api_key"] == r2["api_key"] == "sk-user"   # 同一把 key
    assert r1["provider"] == r2["provider"] == "dashscope"


def test_exclude_source_skips_all_its_endpoints(main_mod, monkeypatch):
    # 降级链含同源两个 model + 另一源；排除该源 → 其名下所有端点一并跳过，落到下一个源
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": ["dashscope:glm-5", "dashscope:qwen-max", "deepseek"]},
        user_cfg={"u1": {"deepseek": {"api_key": "sk-ds", "model_name": "deepseek-v4", "base_url": "https://ds"}}},
        system_cfg={},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1", exclude_providers=["dashscope"])
    assert rt["_source"] == "user"
    assert rt["provider"] == "deepseek"
    assert rt["model_name"] == "deepseek-v4"


# ─────────────── F. auth_service: 用户级 models 往返 + 兜底 ───────────────

def test_user_models_roundtrip(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(
        uid, "dashscope", api_key="sk-u", model_name="glm-5",
        base_url="https://d", models=["glm-5", "qwen-max"],
    )
    cfg = service.get_user_llm_config(uid)
    assert cfg["dashscope"]["model_name"] == "glm-5"
    assert cfg["dashscope"]["models"] == ["glm-5", "qwen-max"]
    assert cfg["dashscope"]["api_key"] == "sk-u"


def test_user_models_backfill_from_model_name(tmp_path):
    # 不传 models → 退化为 [model_name]（模拟老行语义）
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "deepseek", api_key="k", model_name="deepseek-v4")
    cfg = service.get_user_llm_config(uid)
    assert cfg["deepseek"]["models"] == ["deepseek-v4"]


def test_user_models_default_prepended_and_deduped(tmp_path):
    # 默认 model 不在 models 里 → 补到最前；重复去重
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(
        uid, "dashscope", api_key="k", model_name="glm-5",
        models=["qwen-max", "qwen-max", "glm-5"],
    )
    cfg = service.get_user_llm_config(uid)
    assert cfg["dashscope"]["models"] == ["glm-5", "qwen-max"]


# ─────── G. Option B：routing 落盘只存裸 provider + model 解耦到 feature_model 映射 ───────
# 根治 H1：~10 处直接读 llm_feature_routing.json 的后台消费方只认裸 provider，
# 故写入侧必须把 "源:model" 拆开，文件里永不出现 ':'。

def test_split_routing_endpoints(main_mod):
    bare, mm = main_mod._split_routing_endpoints({
        "smart_reply": "dashscope:qwen-max",
        "weekly_report": "deepseek",
        "darwin_eval": ["dashscope:glm-5", "deepseek"],
        "_default": "minimax",
    })
    # 裸 routing 不含任何 ':'（护住直接读者）
    assert bare["smart_reply"] == "dashscope"
    assert bare["weekly_report"] == "deepseek"
    assert bare["darwin_eval"] == ["dashscope", "deepseek"]
    assert bare["_default"] == "minimax"
    for v in bare.values():
        flat = v if isinstance(v, list) else [v]
        assert all(":" not in x for x in flat)
    # model 覆盖被拆到独立映射
    assert mm["smart_reply"] == "qwen-max"
    assert mm["darwin_eval"] == "glm-5"   # list 取首元素 model
    assert "weekly_report" not in mm


def test_feature_model_map_applied_system(main_mod, monkeypatch):
    # M3：bare routing + feature_model 映射 → 系统兜底路径用映射里的 model
    _patch(
        main_mod, monkeypatch,
        routing={"darwin_eval": "dashscope"},          # 裸 provider
        user_cfg={},
        system_cfg={"dashscope": {"api_key": "sk-sys", "model_name": "glm-5", "models": ["glm-5", "qwen-max"]}},
        model_map={"darwin_eval": "qwen-max"},          # 解耦的 model 覆盖
    )
    rt = main_mod.resolve_feature_llm_runtime("darwin_eval", user_id=None)
    assert rt["_source"] == "system"
    assert rt["provider"] == "dashscope"
    assert rt["model_name"] == "qwen-max"               # 映射覆盖生效


def test_feature_model_map_applied_user(main_mod, monkeypatch):
    # bare routing + feature_model 映射 → 用户级路径也用映射 model + 用户 key
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "dashscope"},
        user_cfg={"u1": {"dashscope": {"api_key": "sk-user", "model_name": "glm-5", "base_url": "https://d"}}},
        system_cfg={},
        model_map={"smart_reply": "qwen-max"},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "user"
    assert rt["api_key"] == "sk-user"
    assert rt["model_name"] == "qwen-max"


def test_legacy_ref_in_routing_still_tolerant(main_mod, monkeypatch):
    # 向后兼容：即使 routing 值残留 "源:model"（老数据/手工），resolver 仍能拆并生效
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "dashscope:qwen-max"},
        user_cfg={"u1": {"dashscope": {"api_key": "sk-user", "model_name": "glm-5", "base_url": "https://d"}}},
        system_cfg={},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["model_name"] == "qwen-max" and rt["_source"] == "user"


# ─────── H. per-model 记录：源下每个 model 单独增删/保存（用户级）───────

def test_add_user_model_individual(tmp_path):
    service, uid = _make_service(tmp_path)
    # 先存源凭据（默认 glm-5）
    service.set_user_llm_provider(uid, "dashscope", api_key="sk-u", model_name="glm-5", base_url="https://d")
    # 单独加一个 model
    service.add_user_model(uid, "dashscope", "qwen-max")
    cfg = service.get_user_llm_config(uid)
    assert cfg["dashscope"]["models"] == ["glm-5", "qwen-max"]
    assert cfg["dashscope"]["model_name"] == "glm-5"   # 默认不变
    assert cfg["dashscope"]["api_key"] == "sk-u"        # 共用源凭据


def test_add_user_model_set_default(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "dashscope", api_key="sk-u", model_name="glm-5")
    service.add_user_model(uid, "dashscope", "qwen-max", set_default=True)
    cfg = service.get_user_llm_config(uid)
    assert cfg["dashscope"]["model_name"] == "qwen-max"          # 默认切换
    assert cfg["dashscope"]["models"] == ["qwen-max", "glm-5"]   # 默认置首


def test_add_user_model_requires_source(tmp_path):
    service, uid = _make_service(tmp_path)
    with pytest.raises(ValueError) as ei:
        service.add_user_model(uid, "dashscope", "glm-5")   # 源未保存
    assert str(ei.value) == "provider_not_configured"


def test_remove_user_model_and_default_promotes(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "dashscope", api_key="sk-u", model_name="glm-5")
    service.add_user_model(uid, "dashscope", "qwen-max")
    service.add_user_model(uid, "dashscope", "qwen-plus")
    # 删非默认
    service.remove_user_model(uid, "dashscope", "qwen-plus")
    assert service.get_user_llm_config(uid)["dashscope"]["models"] == ["glm-5", "qwen-max"]
    # 删默认 → 顺延
    service.remove_user_model(uid, "dashscope", "glm-5")
    cfg = service.get_user_llm_config(uid)
    assert cfg["dashscope"]["model_name"] == "qwen-max"
    assert cfg["dashscope"]["models"] == ["qwen-max"]


def test_set_source_preserves_models(tmp_path):
    # 只改源凭据(models=None) → 已配 model 列表保留不丢
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "dashscope", api_key="sk-old", model_name="glm-5")
    service.add_user_model(uid, "dashscope", "qwen-max")
    # 轮换 key，不传 models
    service.set_user_llm_provider(uid, "dashscope", api_key="sk-new", base_url="https://d2")
    cfg = service.get_user_llm_config(uid)
    assert cfg["dashscope"]["api_key"] == "sk-new"
    assert cfg["dashscope"]["base_url"] == "https://d2"
    assert cfg["dashscope"]["models"] == ["glm-5", "qwen-max"]   # 保留
    assert cfg["dashscope"]["model_name"] == "glm-5"             # 默认沿用


def test_norm_models_default_first(tmp_path):
    from auth_service import AuthService
    assert AuthService._norm_models("glm-5", ["qwen-max", "glm-5", "qwen-max"]) == ["glm-5", "qwen-max"]
    assert AuthService._norm_models("", ["a", "a", "b"]) == ["a", "b"]
    assert AuthService._norm_models("x", []) == ["x"]
