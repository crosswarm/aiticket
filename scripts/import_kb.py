#!/usr/bin/env python3
"""
KB 导入脚本 — 把 KB/ 目录下的 Markdown 文档向量化并存入 ChromaDB

用法：
    python scripts/import_kb.py              # 导入 KB/ 目录
    python scripts/import_kb.py --dir /path  # 指定目录
    python scripts/import_kb.py --reset      # 清空后重新导入
"""
import sys
import os

# 确保后端模块可以被导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'APP', 'backend'))

def main():
    import argparse
    parser = argparse.ArgumentParser(description='导入 KB 文档到向量库')
    parser.add_argument('--dir', default='KB', help='KB 文档目录（默认：KB/）')
    parser.add_argument('--reset', action='store_true', help='清空向量库后重新导入')
    args = parser.parse_args()

    # 设置必要的环境变量默认值
    os.environ.setdefault('DATA_DIR', '/data')
    os.environ.setdefault('CHROMA_MODE', 'persistent')

    try:
        from kb_compile_service import KBCompileService
        service = KBCompileService()
        if args.reset:
            print("清空向量库...")
            service.reset()
        print(f"开始导入 {args.dir}/ ...")
        result = service.compile_directory(args.dir)
        print(f"✅ 导入完成：{result.get('indexed', 0)} 篇文档")
    except ImportError as e:
        print(f"❌ 模块导入失败：{e}")
        print("请确保已安装依赖：pip install -r APP/backend/requirements.txt")
        sys.exit(1)

if __name__ == '__main__':
    main()
