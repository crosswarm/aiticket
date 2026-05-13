"""
Idempotent DB schema initializer. Safe to run on every startup.
Usage: python -m bootstrap.init_db [--data-dir /data]
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def init_auth_db(data_dir: Path) -> None:
    db_path = data_dir / "auth.db"
    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                role TEXT DEFAULT 'user',
                current_project TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                user_agent TEXT DEFAULT '',
                ip TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS jira_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                jira_username TEXT NOT NULL,
                cookie_data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id)
            );

            CREATE TABLE IF NOT EXISTS pm_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                pm_username TEXT DEFAULT '',
                cookie_data TEXT NOT NULL,
                tenant_info TEXT NOT NULL DEFAULT '0000',
                proxy_endpoint TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id)
            );

            CREATE TABLE IF NOT EXISTS agent_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                settings_json TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS skill_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                label TEXT DEFAULT 'skill',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                last_used_at TIMESTAMP
            );
        """)
        con.commit()
        print(f"[init_db] auth.db OK ({db_path})")
    finally:
        con.close()


def init_jobmaster_db(data_dir: Path) -> None:
    db_path = data_dir / "jobmaster.db"
    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload TEXT DEFAULT '{}',
                result TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        con.commit()
        print(f"[init_db] jobmaster.db OK ({db_path})")
    finally:
        con.close()


def init_dirs(data_dir: Path) -> None:
    for sub in ["sqlite", "chroma", "chroma_kb", "kb", "reply_trainer", "logs"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    print(f"[init_db] data dirs OK ({data_dir})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    init_dirs(data_dir)
    init_auth_db(data_dir / "sqlite")
    init_jobmaster_db(data_dir / "sqlite")
    print("[init_db] 初始化完成")


if __name__ == "__main__":
    main()
