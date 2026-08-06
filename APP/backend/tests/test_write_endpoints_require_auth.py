"""写端点鉴权防回归。

背景：offline-deploy 没有全局鉴权中间件——`_is_protected_api_path`（main.py:241）
定义了却零调用点，鉴权实际发生在各端点自己的 `require_authenticated_user`。
于是一批会产生**外部不可逆副作用**的写端点长期处于无鉴权状态，其中
`/api/board/batch-approve-replies` 会走 pending_approval_store.approve →
jira_service.reply_and_close_via_transition，**真发客户工单并关单**。
叠加 `allow_origins=["*"] + allow_credentials=True`（main.py:197），
任意站点都能让访客浏览器触发。

本测试钉死这批端点必须鉴权。新增同类端点时请一并登记到 PROTECTED_WRITE_ENDPOINTS。

刻意用 AST 静态分析而不是 TestClient：`import main` 会初始化 ChromaDB 与 bge，
在 CI/开发机上既慢又容易与运行中的实例抢锁（历史事故：僵尸进程持锁导致
新 uvicorn 卡在 U 状态）。静态断言"守卫存在且前置"足以防住本类回归。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

# 会产生外部不可逆副作用（改客户 Jira / 发客户工单）的写端点
PROTECTED_WRITE_ENDPOINTS = [
    "/api/board/batch-approve-replies",  # → reply_and_close_via_transition，真发+关单
    "/api/board/batch-reject-replies",
    "/api/board/move-issue",             # sync_jira=true 时改 Jira 状态
    "/api/board/batch-move",             # 同上，批量
    "/api/board/move-issue-jira",        # 直接调 Jira REST 移动项目
    "/api/jira/action",                  # assign / reply / reply_and_close
    # KB 文档操作：会往知识库写文件、改文件、删索引记录，
    # 且下载会读出原始文档。/api/kb/taxonomy 返回完整 BIP 产品分类（190 label /
    # 1695 application），属内部产品结构，与本组保持同一鉴权口径。
    "/api/kb/upload",                    # 往 KB 写文件
    "/api/kb/document",                  # 读原始文档
    "/api/kb/document/version",          # 读历史版本
    "/api/kb/document/versions",         # 列历史版本
    "/api/kb/document/replace",          # 覆盖已有文档
    "/api/kb/prune-missing",             # 删索引记录（管理员）
    "/api/kb/taxonomy",                  # 内部产品分类
]

_GUARDS = {"require_authenticated_user", "require_admin_user"}


def _collect_endpoints() -> dict[str, dict]:
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if not (isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            out[dec.args[0].value] = {
                "name": node.name,
                "lineno": node.lineno,
                "request_params": [
                    a.arg
                    for a in node.args.args
                    if isinstance(a.annotation, ast.Name) and a.annotation.id == "Request"
                ],
                # 守卫必须出现在函数体前 3 个语句内，不能埋在某个 if 分支深处
                "early_guards": sorted(
                    {
                        n.func.id
                        for st in node.body[:3]
                        for n in ast.walk(st)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and n.func.id in _GUARDS
                    }
                ),
            }
    return out


@pytest.fixture(scope="module")
def endpoints() -> dict[str, dict]:
    return _collect_endpoints()


@pytest.mark.parametrize("path", PROTECTED_WRITE_ENDPOINTS)
def test_write_endpoint_is_registered(path: str, endpoints: dict[str, dict]) -> None:
    """端点还在（重命名/删除时提醒更新本清单）。"""
    assert path in endpoints, f"{path} 未在 main.py 中注册——若已重命名，请同步更新本测试清单"


@pytest.mark.parametrize("path", PROTECTED_WRITE_ENDPOINTS)
def test_write_endpoint_takes_request(path: str, endpoints: dict[str, dict]) -> None:
    """必须拿得到 Request，否则无从取 session。"""
    ep = endpoints[path]
    assert ep["request_params"], (
        f"{path}（{ep['name']} @ main.py:{ep['lineno']}）签名里没有 Request 参数，"
        f"无法鉴权。body model 已占用 request 这个名字时，按仓库惯例用 raw_request: Request。"
    )


@pytest.mark.parametrize("path", PROTECTED_WRITE_ENDPOINTS)
def test_write_endpoint_guards_early(path: str, endpoints: dict[str, dict]) -> None:
    """守卫必须前置——放在函数体前 3 个语句内，早于任何 Jira 调用。"""
    ep = endpoints[path]
    assert ep["early_guards"], (
        f"{path}（{ep['name']} @ main.py:{ep['lineno']}）函数体前 3 个语句里没有 "
        f"{' / '.join(sorted(_GUARDS))}。该端点会产生外部不可逆副作用（改客户 Jira / "
        f"发客户工单），无鉴权等于对整个内网开放。"
    )
