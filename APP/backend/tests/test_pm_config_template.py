"""PM 配置模板的守卫。

## 背景

172 实测：PM 会话「能绑定但取不到数据」。根因是 `config/pm_config.yaml`
在跨血缘移植时漏掉了——主项目里它是入库的，deployable 与 offline-deploy 都没有，
于是 `/api/pm/modules` 等端点直接 500（FileNotFoundError），
而 `PM_BASE_URL` 又为空，代码回落到写死的占位域名 `pm.example.com`（DNS 解析不了）。

「能绑定」是假象：绑定只是把 cookie 写进本地钱包文件，全程不碰 PM 服务器。

## 本文件锁住什么

照 deployment.yaml 的成例：**模板入库、真实配置每实例自配**。
deployable 的 main 会推公开 GitHub，所以模板里绝不能出现内网域名、
真实产品线 UUID 或员工工号。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
TEMPLATE = BACKEND / "config" / "pm_config.yaml.example"
REAL = BACKEND / "config" / "pm_config.yaml"

# 会暴露内网/组织信息的模式
_SECRET_PATTERNS = [
    (r"yyrd\.com", "内网域名"),
    (r"\b172\.20\.\d+\.\d+\b", "内网 IP"),
    (r"\bgfjira\b", "内网 Jira 主机名"),
    (r"\bpmf\b", "内网 PM 主机名"),
]


def test_template_exists():
    assert TEMPLATE.is_file(), (
        "缺少 config/pm_config.yaml.example —— 没有它，新部署无从知道该配什么，"
        "PM 端点会直接 FileNotFoundError 500"
    )


def test_template_has_no_internal_identifiers():
    """★ 模板会进公开仓库，不能带任何内网/组织信息。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    for pattern, label in _SECRET_PATTERNS:
        assert not re.search(pattern, text, re.I), f"模板里出现{label}：{pattern}"


def test_template_has_no_real_uuid_or_employee_id():
    """产品线 UUID 与默认分析人工号属于组织内部标识，模板里必须是占位符。"""
    text = TEMPLATE.read_text(encoding="utf-8")

    def _is_placeholder(v: str) -> bool:
        """全零（可含连字符）视为占位符，例如 00000000-0000-... / 0000000000。"""
        return set(v) <= {"0", "-"}

    for m in re.finditer(r'line_id:\s*"([^"]*)"', text):
        val = m.group(1)
        if _is_placeholder(val):
            continue
        assert not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", val), (
            f"line_id 仍是真实 UUID：{val}"
        )

    for m in re.finditer(r'default_analyst:\s*"([^"]*)"', text):
        val = m.group(1)
        if _is_placeholder(val):
            continue
        assert not re.fullmatch(r"\d{8,}", val), f"default_analyst 仍是真实工号：{val}"


def test_template_keeps_keys_the_code_depends_on():
    """模板要能直接跑通：代码实际读取的键必须都在。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    for key in ("pm_system:", "base_url:", "api_prefix:", "tenant_info:", "modules:", "token_file:"):
        assert key in text, f"模板缺少代码依赖的键：{key}"


def test_template_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "pm_system" in data
    assert data["pm_system"].get("modules"), "模板必须带至少一个模块定义"


def test_real_config_is_git_ignored():
    """真实配置含内网域名，绝不能入库（deployable main 会推公开 GitHub）。"""
    r = subprocess.run(
        ["git", "check-ignore", "-q", "APP/backend/config/pm_config.yaml"],
        cwd=REPO, capture_output=True,
    )
    assert r.returncode == 0, "config/pm_config.yaml 必须被 .gitignore 覆盖"


def test_real_config_not_tracked():
    r = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "APP/backend/config/pm_config.yaml"],
        cwd=REPO, capture_output=True,
    )
    assert r.returncode != 0, "config/pm_config.yaml 不应被 git 跟踪"
