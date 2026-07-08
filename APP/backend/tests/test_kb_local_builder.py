import json
import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_local_builder_generates_relative_manifest_and_index_files(tmp_path: Path):
    from kb_local_builder import KBLocalBuilder

    project_root = tmp_path / "repo"
    kb_root = project_root / "KB"
    topic_file = project_root / "topic.md"

    source_file = kb_root / "流程中心" / "帮助文档" / "流程监控说明.md"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "# 流程监控说明\n\n流程监控支持未来审批人查询和人工干预。",
        encoding="utf-8",
    )
    topic_file.write_text(
        "# 主题\n\n- [TOP-WF] 工作流\n  - [TOP-WF.MONITOR] 流程监控\n",
        encoding="utf-8",
    )

    builder = KBLocalBuilder(project_root=project_root, kb_root=kb_root, topic_file=topic_file)

    result = builder.build()

    manifest_path = kb_root / "INDEX" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content_id, item = next(iter(manifest["contents"].items()))
    converted_path = project_root / item["converted_path"]
    index_path = kb_root / "INDEX" / "FILES" / content_id / "INDEX.md"

    assert result["source_files"] == 1
    assert item["source_path"] == "KB/流程中心/帮助文档/流程监控说明.md"
    assert item["converted_path"] == "KB/OUTPUT/converted/流程中心/帮助文档/流程监控说明.md"
    assert item["parse_status"] == "parsed"
    assert converted_path.exists()
    assert "未来审批人查询" in converted_path.read_text(encoding="utf-8")
    assert index_path.exists()


def test_local_builder_defaults_to_data_dir_kb(tmp_path: Path, monkeypatch):
    from kb_local_builder import KBLocalBuilder

    project_root = tmp_path / "repo"
    data_dir = tmp_path / "runtime-data"
    kb_root = data_dir / "kb"
    topic_file = project_root / "topic.md"
    source_file = kb_root / "流程中心" / "帮助文档" / "流程监控说明.md"

    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("# 流程监控说明\n\n流程监控支持未来审批人查询和人工干预。", encoding="utf-8")
    topic_file.parent.mkdir(parents=True, exist_ok=True)
    topic_file.write_text("# 主题\n", encoding="utf-8")

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("AITICKET_DATA_ROOT", raising=False)
    monkeypatch.delenv("KB_ROOT", raising=False)

    builder = KBLocalBuilder(project_root=project_root, topic_file=topic_file)
    result = builder.build()

    manifest = json.loads((kb_root / "INDEX" / "manifest.json").read_text(encoding="utf-8"))
    item = next(iter(manifest["contents"].values()))

    assert builder.kb_root == kb_root.resolve()
    assert result["content_count"] == 1
    assert item["converted_path"] == "KB/OUTPUT/converted/流程中心/帮助文档/流程监控说明.md"
    assert (kb_root / "OUTPUT" / "converted" / "流程中心" / "帮助文档" / "流程监控说明.md").exists()


def test_local_builder_writes_degraded_stub_when_parser_returns_error(tmp_path: Path, monkeypatch):
    from kb_local_builder import KBLocalBuilder

    project_root = tmp_path / "repo"
    kb_root = project_root / "KB"
    topic_file = project_root / "topic.md"

    source_file = kb_root / "流程中心" / "截图" / "审批流.png"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"fake-image")
    topic_file.write_text("# 主题\n", encoding="utf-8")

    builder = KBLocalBuilder(project_root=project_root, kb_root=kb_root, topic_file=topic_file)

    class FakeResult:
        text = ""
        parse_status = "degraded"
        parse_error = "ocr unavailable"
        parser = "fake"
        metadata_only = True

    monkeypatch.setattr(builder, "_parse_file", lambda path: FakeResult())

    builder.build()

    manifest = json.loads((kb_root / "INDEX" / "manifest.json").read_text(encoding="utf-8"))
    item = next(iter(manifest["contents"].values()))
    converted = (project_root / item["converted_path"]).read_text(encoding="utf-8")

    assert item["parse_status"] == "degraded"
    assert item["parse_error"] == "ocr unavailable"
    assert "ocr unavailable" in converted


def test_local_builder_preserves_existing_manifest_when_raw_sources_missing(tmp_path: Path):
    from kb_local_builder import KBLocalBuilder

    project_root = tmp_path / "repo"
    kb_root = project_root / "KB"
    topic_file = project_root / "topic.md"
    manifest_path = kb_root / "INDEX" / "manifest.json"

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-03-23T10:00:00",
                "source_files": 1,
                "converted_files": 1,
                "contents": {
                    "CNT-0001": {
                        "content_id": "CNT-0001",
                        "source_path": "KB/流程中心/帮助文档/流程监控说明.md",
                        "source_rel_path": "流程中心/帮助文档/流程监控说明.md",
                        "converted_path": "KB/OUTPUT/converted/流程中心/帮助文档/流程监控说明.md",
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    topic_file.write_text("# 主题\n", encoding="utf-8")

    builder = KBLocalBuilder(project_root=project_root, kb_root=kb_root, topic_file=topic_file)

    result = builder.build()

    assert result["content_count"] == 1
    assert result["source_files"] == 1
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["contents"]["CNT-0001"]["source_path"] == "KB/流程中心/帮助文档/流程监控说明.md"
