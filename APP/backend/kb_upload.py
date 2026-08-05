"""KB 知识上传的落盘逻辑。

从 main.py 抽出来，因为 HTTP 层没法单测（import main 会拉起整个应用，
还容易留下持有 ChromaDB 锁的僵尸进程）。这里是纯函数，输入输出都是普通值。

归属由**目录结构**承载：文件落到 ``KB/<label>/<application>/``，
索引构建时由 bip_taxonomy 解析回 label / application，
所以新上传的知识天生带标，不会重演存量 chunk 全是 _global 的局面。
"""

from __future__ import annotations

import re
from pathlib import Path

from bip_taxonomy import BipTaxonomy, safe_dirname

# 能被 KB 解析出正文的格式；其余格式也允许上传，但只留元数据（parse degraded）
PARSABLE_EXTS = {".md", ".txt", ".csv", ".sql", ".html", ".xml", ".docx", ".pptx", ".xlsx"}
UPLOAD_EXTS = PARSABLE_EXTS | {".pdf"}
MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 20

_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')

# 主数据的 application 层混着大量非正式条目。租户自建的靠 code 前缀（AT1/GT）能认出来，
# 但还有一批 code 看不出、只能从名字判断的草稿件——
# 「0724特征验证(勿动)」「0816专业版-勿动」「0租户应用」之类。
# 只用于上传下拉框的展示过滤，**不影响归属解析**（解析走 applications 全集）。
_SCRATCH_NAME = re.compile(r"(勿动|勿删|请勿|测试|验证|临时|试用|demo|test|0租户|零租户|自建|专业版-)", re.I)


def is_listable_application(code: str, app: dict) -> bool:
    if app.get("tenant_built"):
        return False
    return not _SCRATCH_NAME.search(app.get("name") or "")


class UploadRejected(Exception):
    """调用方应转成 4xx 的输入问题。"""


def safe_filename(raw: str) -> str:
    """上传的文件名会直接参与拼路径，只取基名并剔除危险字符。"""
    name = Path(raw or "").name.strip()
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    name = name.lstrip(". ") or "untitled"
    return name[:180]


def resolve_target_dir(
    kb_root: Path, taxonomy: BipTaxonomy, label_code: str, application_code: str = ""
) -> tuple[Path, dict, dict | None]:
    """校验归属并算出落盘目录。返回 (目录, label 信息, application 信息)。

    目录名一律从主数据按 code 反查，不采信调用方传来的名字——
    这既是防越界，也保证目录名与解析侧完全一致。
    """
    label = taxonomy.labels.get(label_code)
    if not label:
        raise UploadRejected(f"未知的 label_code: {label_code}")

    app_info = None
    if application_code:
        app = taxonomy.applications.get(application_code)
        if not app:
            raise UploadRejected(f"未知的 application_code: {application_code}")
        if app.get("label") != label_code:
            raise UploadRejected(f"application {application_code} 不属于 label {label_code}")
        app_info = {"code": application_code, "name": app.get("name", "")}

    target = kb_root / safe_dirname(label["name"])
    if app_info and app_info["name"]:
        target = target / safe_dirname(app_info["name"])
    return target, {"code": label_code, "name": label["name"]}, app_info


def _unique_path(target: Path) -> Path:
    """同名不覆盖——KB 里同名文档很常见，覆盖会静默丢知识。"""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 1000):
        candidate = target.with_name(f"{stem}({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise UploadRejected(f"同名文件过多：{target.name}")


def save_uploads(
    kb_root: Path,
    taxonomy: BipTaxonomy,
    label_code: str,
    application_code: str,
    files: list[tuple[str, bytes]],
) -> dict:
    """把上传的文件按归属写入 KB。``files`` 是 (原始文件名, 内容) 列表。"""
    if not taxonomy.available:
        raise UploadRejected("BIP 分类快照不可用，无法校验归属")
    if not files:
        raise UploadRejected("没有收到文件")
    if len(files) > MAX_FILES:
        raise UploadRejected(f"单次最多 {MAX_FILES} 个文件")

    target_dir, label_info, app_info = resolve_target_dir(
        kb_root, taxonomy, label_code, application_code
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    skipped: list[dict] = []
    for raw_name, content in files:
        filename = safe_filename(raw_name)
        ext = Path(filename).suffix.lower()
        if ext not in UPLOAD_EXTS:
            skipped.append({"filename": filename, "reason": f"不支持的格式 {ext or '(无扩展名)'}"})
            continue
        if len(content) > MAX_BYTES:
            skipped.append({"filename": filename, "reason": f"超过 {MAX_BYTES // 1024 // 1024}MB"})
            continue
        if not content:
            skipped.append({"filename": filename, "reason": "空文件"})
            continue

        target = _unique_path(target_dir / filename)
        target.write_bytes(content)
        saved.append({
            "filename": target.name,
            "rel_path": target.relative_to(kb_root.parent).as_posix(),
            "bytes": len(content),
            "parsable": ext in PARSABLE_EXTS,
        })

    return {
        "status": "success" if saved else "error",
        "label": label_info,
        "application": app_info,
        "target_dir": target_dir.relative_to(kb_root.parent).as_posix(),
        "saved": saved,
        "skipped": skipped,
        "next_step": "调用 POST /api/kb/refresh 让新文件进入索引（转换+嵌入耗时较长）",
    }


def build_taxonomy_tree(taxonomy: BipTaxonomy, domain_cloud: str | None = None) -> dict:
    """上传时可选的归属树：领域云 > label > application。

    只暴露正式应用——1695 个 application 里混着大量租户自建应用和草稿件，
    实测数字化建模一个 label 下就有 96 个，「0租户应用」「0816专业版-勿动」
    这种排在最前面，放进下拉框只会干扰选择。
    """
    apps_by_label: dict[str, list[dict]] = {}
    for code, app in taxonomy.applications.items():
        if not is_listable_application(code, app):
            continue
        apps_by_label.setdefault(app.get("label", ""), []).append(
            {"code": code, "name": app.get("name", "")}
        )

    labels = []
    for code, label in taxonomy.labels.items():
        if domain_cloud and label.get("domain_cloud") != domain_cloud:
            continue
        labels.append({
            "code": code,
            "name": label.get("name", ""),
            "domain_cloud": label.get("domain_cloud", ""),
            "domain_cloud_name": label.get("domain_cloud_name", ""),
            "applications": sorted(apps_by_label.get(code, []), key=lambda a: a["name"]),
        })
    labels.sort(key=lambda x: (x["domain_cloud_name"], x["name"]))
    return {
        "domain_clouds": taxonomy.domain_clouds,
        "labels": labels,
        "preferred_domain_clouds": ["PFC", "CPC"],
    }
