#!/usr/bin/env python3
"""导出 BIP 产品主数据分类快照（domain_cloud / label / application / service 四层）。

用途
----
KB 知识库的归属打标与召回加权需要一份权威的产品分类字典。
172 是气隙内网，连不到 `dbproxy.diwork.com`，所以本脚本**只在有网机器上运行**，
产出的 `data/bip_taxonomy.json` 随 git 带进内网。

四层挂载关系（主数据库里靠字段内嵌，不是关系表；
`sys_label_application_relation` 是空表，别去查它）::

    sys_domain_cloud                    领域云
        ↑ sys_label.domain_cloud_code
    sys_label (label_type='domain')     领域 = 知识区隔粒度
        ↑ sys_application.label_domain
    sys_application                     应用
        ↑ sys_service.application_code
    sys_service                         服务

运行::

    BIP_DB_PASSWORD=xxx python3 export_bip_taxonomy.py
    BIP_DB_PASSWORD=xxx python3 export_bip_taxonomy.py --out /tmp/t.json

依赖 pymysql（仅本脚本需要，不进 requirements，避免污染离线部署环境）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DB_HOST = os.environ.get("BIP_DB_HOST", "dbproxy.diwork.com")
DB_PORT = int(os.environ.get("BIP_DB_PORT", "12999"))
DB_USER = os.environ.get("BIP_DB_USER", "iuap_benchservice")
DB_NAME = os.environ.get("BIP_DB_NAME", "iuap_apcom_benchservice")

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "bip_taxonomy.json"


def connect():
    try:
        import pymysql
    except ImportError:
        sys.exit("需要 pymysql：pip install pymysql")
    password = os.environ.get("BIP_DB_PASSWORD")
    if not password:
        sys.exit("请通过环境变量 BIP_DB_PASSWORD 提供密码（不要写进代码或命令历史）")
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=password,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=15,
        read_timeout=180,
    )


def fetch(conn, sql: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def export(conn) -> dict:
    # 领域云：ds=0 过滤逻辑删除
    domain_clouds = {
        r["c"]: r["nm"]
        for r in fetch(conn, """
            SELECT domain_cloud_code c, domain_cloud_name nm
            FROM sys_domain_cloud WHERE ds=0 AND domain_cloud_code IS NOT NULL
        """)
        if r["c"]
    }

    # label：只取 label_type='domain'（另一类 'type' 是交叉业务标签，不是层级）
    labels: dict[str, dict] = {}
    for r in fetch(conn, """
        SELECT label_code c, label_name nm, domain_cloud_code dc, domain_cloud_name dcn
        FROM sys_label WHERE ds=0 AND label_type='domain' AND label_code IS NOT NULL
    """):
        if not r["c"]:
            continue
        labels[r["c"]] = {
            "name": r["nm"] or "",
            "domain_cloud": r["dc"] or "",
            "domain_cloud_name": r["dcn"] or "",
        }

    # application：只取上架的。租户自建的测试应用也在这张表里（AT1xxx / GTxxx 编码），
    # 保留但打 tenant_built 标记，交由消费方决定是否忽略。
    applications: dict[str, dict] = {}
    for r in fetch(conn, """
        SELECT application_code c, application_name nm, label_domain ld
        FROM sys_application WHERE ds=0 AND launch=1 AND application_code IS NOT NULL
    """):
        code = r["c"]
        if not code:
            continue
        entry = {"name": r["nm"] or "", "label": r["ld"] or ""}
        if code.startswith(("AT1", "GT")):
            entry["tenant_built"] = True
        applications[code] = entry

    # service：只保留挂在已知 application 下的，去掉悬空引用
    services: dict[str, dict] = {}
    for r in fetch(conn, """
        SELECT service_code c, service_name nm, application_code ac
        FROM sys_service WHERE ds=0 AND service_code IS NOT NULL
    """):
        code = r["c"]
        if not code or r["ac"] not in applications:
            continue
        services[code] = {"name": r["nm"] or "", "application": r["ac"]}

    return {
        "_meta": {
            "source": f"{DB_NAME} (BIP 产品主数据)",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filters": "ds=0；label_type='domain'；application launch=1；service 需挂在已知 application 下",
            "hierarchy": "domain_cloud > label > application > service",
            "note": "label 是知识区隔粒度；application 层混有租户自建应用（tenant_built 标记）",
            "regenerate": "BIP_DB_PASSWORD=xxx python3 scripts/export_bip_taxonomy.py",
            "counts": {
                "domain_clouds": len(domain_clouds),
                "labels": len(labels),
                "applications": len(applications),
                "services": len(services),
            },
        },
        "domain_clouds": domain_clouds,
        "labels": labels,
        "applications": applications,
        "services": services,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 BIP 产品主数据分类快照")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出路径")
    args = parser.parse_args()

    conn = connect()
    try:
        payload = export(conn)
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # 紧凑写入：15000+ service 用缩进会让文件翻三倍
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    counts = payload["_meta"]["counts"]
    size_kb = args.out.stat().st_size / 1024
    print(f"已写入 {args.out}（{size_kb:.0f} KB）")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
