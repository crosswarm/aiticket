"""
Create the first admin user interactively.
Usage: python -m bootstrap.seed_admin [--data-dir /data] [--username admin] [--password ...]
"""

import argparse
import hashlib
import os
import secrets
import sqlite3
import sys
from pathlib import Path


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def create_admin(db_path: Path, username: str, password: str, display_name: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        existing = con.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            print(f"[seed_admin] 用户 '{username}' 已存在，跳过")
            return
        pw_hash = hash_password(password)
        con.execute(
            "INSERT INTO users (username, password_hash, display_name, role) VALUES (?, ?, ?, 'admin')",
            (username, pw_hash, display_name or username),
        )
        con.commit()
        print(f"[seed_admin] Admin 用户 '{username}' 创建成功")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data", type=Path)
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()

    db_path = args.data_dir / "sqlite" / "auth.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} 不存在，请先运行 init_db")
        sys.exit(1)

    username = args.username or input("用户名 [admin]: ").strip() or "admin"
    password = args.password or input("密码: ").strip()
    if not password:
        print("ERROR: 密码不能为空")
        sys.exit(1)
    display_name = args.display_name or input(f"显示名 [{username}]: ").strip()

    create_admin(db_path, username, password, display_name)


if __name__ == "__main__":
    main()
