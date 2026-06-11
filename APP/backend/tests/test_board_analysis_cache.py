import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_file_cache_analysis_recovers_from_empty_cache_file(tmp_path, monkeypatch):
    import board_service_chroma

    # _file_cache_analysis 实际用 BASE_DIR 解析 data_cache（非 PROJECT_ROOT），
    # 两者都 patch 防止写到真实缓存
    monkeypatch.setattr(board_service_chroma, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(board_service_chroma, "BASE_DIR", str(tmp_path / "APP" / "backend"))

    cache_dir = tmp_path / "APP" / "backend" / "data_cache"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "analysis_cache.json"
    cache_file.write_text("", encoding="utf-8")

    worker = board_service_chroma.AIAnalysisWorker.__new__(board_service_chroma.AIAnalysisWorker)
    # issue_title 必传：ef89f8d 起空 title 写入被 quality gate 拦截（403 污染防御）
    worker._file_cache_analysis(
        "LCZX-TEST-2001",
        {
            "recommended_team": "云平台-流程中心",
            "recommended_role": "产品经理",
        },
        issue_title="测试工单标题",
    )

    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved["LCZX-TEST-2001"]["recommended_team"] == "云平台-流程中心"
    assert saved["LCZX-TEST-2001"]["recommended_role"] == "产品经理"
    assert saved["LCZX-TEST-2001"]["cache_type"] == "file"
