"""粘贴 cURL 导入会话时的 cookie 解析守卫。

## 背景

172 实测发现某用户的 PM 绑定里 `tenant_info` 存成了 `"0000^"`，比正常值多一个 `^`。
根因：Chrome 在 Windows 上的「Copy as cURL (cmd)」用 `^` 作 cmd 转义符，
而 board.html 的解析正则字符类没有排除它，于是转义符被当成 cookie 值的一部分存了下来。

带脏字符的凭据发到 PM/Jira 会被判定无效，且现象隐蔽——绑定照样"成功"
（绑定只写本地钱包，不校验），要等真正取数据时才失败。

## 做法

`parseCurlForCookies` 是不依赖 DOM 的纯函数，所以这里把它从内联脚本里抽出来
用 node 真跑一遍，而不是只做文本结构断言。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

BOARD_HTML = Path(__file__).resolve().parents[2] / "frontend" / "board.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行前端纯函数")


def _extract_fn(name: str) -> str:
    """从 board.html 内联脚本里抽出一个顶层函数的源码。"""
    html = BOARD_HTML.read_text(encoding="utf-8")
    start = html.index(f"function {name}(")
    depth, i, started = 0, start, False
    while i < len(html):
        if html[i] == "{":
            depth += 1; started = True
        elif html[i] == "}":
            depth -= 1
            if started and depth == 0:
                return html[start : i + 1]
        i += 1
    raise AssertionError(f"没能抽出函数 {name}")


def _run(curl_text: str) -> dict:
    # parseCurlForCookies 依赖同文件里的 _stripCmdCaret，两个都要带上
    src = _extract_fn("_stripCmdCaret") + "\n" + _extract_fn("parseCurlForCookies")
    script = src + "\nconsole.log(JSON.stringify(parseCurlForCookies(" + json.dumps(curl_text) + ")));\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script); path = f.name
    try:
        out = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr[:400]
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        Path(path).unlink(missing_ok=True)


# ── 正常（macOS / Linux）格式 ────────────────────────────────────────────────

def test_parses_unix_style_curl():
    r = _run("""curl 'https://pm.example.com/rest/v1/x' -H 'cookie: yht_access_token=bttAAA111; tenant_info=0000; ycap_06e6ea000524=eyJhbGciOi'""")
    assert r["yht_access_token"] == "bttAAA111"
    assert r["tenant_info"] == "0000"
    assert r["ycap_cookies"]["ycap_06e6ea000524"] == "eyJhbGciOi"


def test_parses_jira_cookies():
    r = _run("""curl 'https://jira.example.com/x' -H 'cookie: JSESSIONID=ABC123DEF456; atlassian.xsrf.token=T1-T2-T3'""")
    assert r["JSESSIONID"] == "ABC123DEF456"
    assert r["xsrf_token"] == "T1-T2-T3"


# ── Windows「Copy as cURL (cmd)」格式 ───────────────────────────────────────

def test_cmd_style_caret_is_not_kept_in_values():
    """★ 回归：cmd 的 ^ 转义符不能混进 cookie 值。

    实测坏数据：tenant_info 被存成 "0000^"。
    """
    r = _run('curl "https://pm.example.com/x" -H "cookie: yht_access_token=bttAAA111^; tenant_info=0000^; ycap_06e6ea000524=eyJhbGciOi^"')
    assert r["tenant_info"] == "0000", f"tenant_info 仍带转义符：{r['tenant_info']!r}"
    assert r["yht_access_token"] == "bttAAA111", f"token 仍带转义符：{r['yht_access_token']!r}"
    for k, v in r["ycap_cookies"].items():
        assert "^" not in v, f"{k} 仍带转义符：{v!r}"


def test_cmd_style_caret_inside_value_is_removed():
    """cmd 用 ^ 转义 & | < > 等，值中间出现的 ^ 同样应剔除。"""
    r = _run('curl "https://pm.example.com/x" -H "cookie: yht_access_token=btt^&AAA; tenant_info=0000"')
    assert "^" not in r["yht_access_token"]


def test_no_cookies_yields_empty_result():
    r = _run("curl 'https://example.com/'")
    assert not r.get("JSESSIONID")
    assert r.get("ycap_cookies") == {}
