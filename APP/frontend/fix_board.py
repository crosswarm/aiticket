import re
with open('board.html', 'r', encoding='utf-8') as f:
    content = f.read()

content = re.sub(r'href="/search\.html"', 'href="search.html"', content)
content = re.sub(r'href="/board\.html"', 'href="board.html"', content)
content = re.sub(r'href="/report\.html"', 'href="report.html"', content)

api_base_script = """
        const getApiBase = () => {
            const hostname = window.location.hostname;
            if (hostname === 'localhost' || hostname === '127.0.0.1') {
                return `http://${hostname}:3000`;
            }
            return '';
        };
        const API_BASE = getApiBase();
"""
if 'const API_BASE' not in content:
    content = content.replace("let boardData = {};", api_base_script + "\n        let boardData = {};")

content = re.sub(r"fetch\(['\"]/api/([^'\"]+)['\"]\)", r"fetch(`${API_BASE}/api/\1`)", content)
content = re.sub(r"fetch\(`/api/([^`]+)`", r"fetch(`${API_BASE}/api/\1`", content)

with open('board.html', 'w', encoding='utf-8') as f:
    f.write(content)
