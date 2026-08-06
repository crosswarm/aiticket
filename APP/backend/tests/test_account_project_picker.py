"""账号设置「默认项目」选择器的结构守卫。

## 修的是什么

172 实测：新用户登录后打开 头像 → 账号设置 → 绑定项目，下拉里**只有「全部项目」**，
选不了任何具体项目。三层原因：

1. **鸡生蛋死锁（主因）**：项目列表的加载被写在
   `if (defProject && defProject !== 'ALL') { ... }` 里面 —— 只有「已经绑定了
   具体项目」的用户才会去拉列表。可新用户 current_project 为空，走 else 分支，
   列表永远不加载，于是永远绑不上项目。
2. **数据源要求 per-user Jira 绑定**：用的是 `/api/board/move-targets/0`
   （`build_request_jira_client` 默认 require_binding=True），172 上 26 个活跃
   用户里 22 个没有 Jira 绑定 → 直接 401。右上角作用域切换器用的
   `/api/board/meta` 则是 require_binding=False 且带兜底列表，所以那边一直正常。
3. Jira 会话大面积过期（运维问题，不在本文件覆盖范围）。

## 为什么用结构断言而不是行为测试

这段逻辑是 board.html 里的内联 JS，抽不出来单测。仓库已有先例
（test_badcase_context.py 用同样方式锁死弹窗 z-index，并真的抓到过遮挡 bug）。
这里锁的是「加载时机」和「数据源」这两个人眼容易看漏、但一改就复发的性质。
"""
from __future__ import annotations

import re
from pathlib import Path

BOARD_HTML = Path(__file__).resolve().parents[2] / "frontend" / "board.html"


def _read() -> str:
    return BOARD_HTML.read_text(encoding="utf-8")


def _account_settings_body(html: str) -> str:
    """截出 openAccountSettings 函数体（到下一个顶层 function 为止）。"""
    start = html.index("async function openAccountSettings(")
    rest = html[start:]
    m = re.search(r"\n        function closeAccountSettings\(", rest)
    assert m, "找不到 openAccountSettings 的结束边界"
    return rest[: m.start()]


def test_project_list_loads_regardless_of_existing_binding():
    """★ 核心回归：项目列表的加载不能被「已绑定具体项目」当作前提。

    死锁的形状是——加载语句出现在 `if (defProject && defProject !== 'ALL')`
    这个条件块【内部】。这里断言：加载发生在该条件判断【之前】。
    """
    body = _account_settings_body(_read())

    cond = body.index("if (defProject && defProject !== 'ALL')")
    fetches = [m.start() for m in re.finditer(r"_acctProjectList\s*=", body)]

    assert fetches, "账号设置里没有给 _acctProjectList 赋值的地方"
    assert any(pos < cond for pos in fetches), (
        "项目列表只在『已绑定具体项目』时才加载 —— 新用户永远看不到列表、"
        "也就永远绑不上项目（鸡生蛋死锁）。加载必须无条件执行。"
    )


def test_account_picker_does_not_require_per_user_jira_binding():
    """账号设置的数据源必须是 /api/board/meta。

    move-targets 需要 per-user Jira 绑定，172 上 22/26 用户没绑定 → 401，
    下拉就永远是空的。meta 不要求绑定且有兜底列表。
    """
    body = _account_settings_body(_read())
    # 只看真实代码，注释里为解释「为何不用 move-targets」而提到它是合理的
    code = "\n".join(
        ln for ln in body.split("\n") if not ln.strip().startswith("//")
    )

    assert "/api/board/meta" in code, "账号设置应改用 /api/board/meta 取项目列表"
    assert "move-targets" not in code, (
        "账号设置不应再【调用】/api/board/move-targets —— 它要求 per-user Jira 绑定，"
        "未绑定用户会拿到 401"
    )


def test_dropdown_reads_account_specific_list():
    """下拉渲染必须读账号设置专用的列表，而不是移动工单那份。"""
    html = _read()
    start = html.index("function _renderAcctProjectDropdown(")
    body = html[start : start + 1200]

    assert "_acctProjectList" in body, "下拉应基于 _acctProjectList 渲染"
    assert "...moveProjectList" not in body, (
        "下拉不应再展开 moveProjectList —— 它只在『移动工单』弹窗打开时才被填充，"
        "从没打开过该弹窗的用户拿到的是空数组"
    )


def test_move_issue_flow_keeps_its_own_source_and_id():
    """★ 反向保护：不能为了修账号设置而破坏「移动工单」。

    移动工单靠 project.id 提交（selectMoveProject 把 id 写进表单），
    而 /api/board/meta 只返回 key/name、没有 id。所以两处必须各用各的数据源：
    移动工单继续用 move-targets，账号设置用 meta。
    """
    html = _read()
    start = html.index("async function openMoveIssueModal(")
    body = html[start : start + 3000]

    assert "move-targets" in body, "移动工单仍应使用 /api/board/move-targets（它返回 id）"
    assert "id: p.id" in body, "移动工单的项目项必须保留 id 字段"
    assert "selectMoveProject(p.id" in html, "移动工单的选中回调必须传 id"


def test_all_projects_option_still_present():
    """『全部项目』这一项要保留 —— 它是合法的默认范围。"""
    html = _read()
    start = html.index("function _renderAcctProjectDropdown(")
    assert "key:'ALL'" in html[start : start + 800]
