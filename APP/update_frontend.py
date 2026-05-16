import os
import re

frontend_dir = os.path.join(os.path.dirname(__file__), 'frontend')
files = ['index.html', 'board.html', 'report.html']

def update_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return

    # Replace absolute hrefs with relative
    content = re.sub(r'href="/search\.html"', 'href="search.html"', content)
    content = re.sub(r'href="/board\.html"', 'href="board.html"', content)
    content = re.sub(r'href="/report\.html"', 'href="report.html"', content)
    content = re.sub(r'href="/"', 'href="index.html"', content)
    
    # Replace single quote hrefs for board.html drawer bug
    content = re.sub(r"href='/search\.html'", "href='search.html'", content)
    
    # Fix `/api/` fetch URLs
    content = re.sub(r'fetch\([\'"]/api/', 'fetch(`${API_BASE}/api/', content)
    content = re.sub(r'fetch\(`\/api/', 'fetch(`${API_BASE}/api/', content)
    content = re.sub(r'href="/api/', 'href="`${API_BASE}/api/', content) # for downloads

    # Dynamic API_BASE logic
    api_base_snippet = """const getApiBase = () => {
        const hostname = window.location.hostname;
        if (hostname === 'localhost' || hostname === '127.0.0.1') {
            return `http://${hostname}:3000`;
        }
        return '';
    };
    const API_BASE = getApiBase();"""
    
    # Replace existing const API_BASE instances
    content = re.sub(r'const API_BASE = "[^"]*";(?:\s*//[^\n]+)?', api_base_snippet, content)
    content = re.sub(r"const API_BASE = '[^']*';(?:\s*//[^\n]+)?", api_base_snippet, content)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Updated {filepath}")

for file in files:
    update_file(os.path.join(frontend_dir, file))
