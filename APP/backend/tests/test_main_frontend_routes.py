import os

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")
FRONTEND_DIR = os.path.normpath(os.path.join(BACKEND_DIR, "../frontend"))


def test_kb_page_route_returns_existing_kb_html_file():
    kb_page_path = os.path.join(FRONTEND_DIR, "kb.html")

    assert kb_page_path.endswith("kb.html")
    assert os.path.exists(kb_page_path)


def test_req_pool_spec_script_route_returns_existing_js_file():
    req_pool_spec_path = os.path.join(FRONTEND_DIR, "req-pool-spec.js")

    assert req_pool_spec_path.endswith("req-pool-spec.js")
    assert os.path.exists(req_pool_spec_path)
