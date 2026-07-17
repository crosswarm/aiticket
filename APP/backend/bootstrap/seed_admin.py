"""
创建 AITicket 的首个（或额外）管理员账号。

关键：本脚本**委托 auth_service** 完成写库，因此写入的数据库文件与密码格式
与运行中的后端**完全一致**（`APP_AUTH_DB_PATH` 指向的库 + pbkdf2_sha256）。
可重复运行（幂等）：同名同密码则跳过；同名不同密码则报错提示改用管理后台。

用法:
    # 容器内（compose 服务名 = aiticket）
    docker compose exec aiticket python -m bootstrap.seed_admin --username admin --password '你的强密码'

    # 原生部署
    python -m bootstrap.seed_admin --username admin --password '你的强密码'

    # 不带参数则交互式输入（密码不回显）
    python -m bootstrap.seed_admin

环境变量:
    APP_AUTH_DB_PATH   认证库路径（容器内通常 /data/app_auth.db）。设置了就用它，
                       与后端保持一致；未设置时用 --data-dir，再退回代码默认。
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

# 支持 `python -m bootstrap.seed_admin`：把 APP/backend 加进 sys.path 以 import auth_service
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth_service import AuthService  # noqa: E402


def _resolve_paths(data_dir: str):
    """返回 (db_path, secret_path)。APP_AUTH_DB_PATH 优先——与后端一致。

    - 已设 APP_AUTH_DB_PATH → 返回 (None, None)，交给 AuthService 读环境变量。
    - 否则给了 --data-dir → 用 <data-dir>/app_auth.{db,key}。
    - 都没有 → (None, None)，AuthService 用代码默认 <backend>/data/app_auth.db。
    """
    if os.environ.get("APP_AUTH_DB_PATH"):
        return None, None
    if data_dir:
        d = Path(data_dir)
        return str(d / "app_auth.db"), str(d / "app_auth.key")
    return None, None


def main() -> None:
    parser = argparse.ArgumentParser(description="创建 AITicket 首个/额外管理员")
    parser.add_argument("--data-dir", default="",
                        help="数据目录（仅当未设 APP_AUTH_DB_PATH 时生效）")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()

    db_path, secret_path = _resolve_paths(args.data_dir)
    svc = AuthService(db_path=db_path, secret_path=secret_path)

    interactive = not args.username
    username = args.username or (input("用户名 [admin]: ").strip() or "admin")
    password = args.password or getpass.getpass("密码: ")
    if not password:
        print("ERROR: 密码不能为空")
        sys.exit(1)
    display_name = args.display_name or (
        input(f"显示名 [{username}]: ").strip() if interactive else ""
    ) or username

    # 幂等：同名同密码已存在则跳过
    if svc.authenticate(username, password):
        print(f"[seed_admin] 用户 '{username}' 已存在且密码一致，跳过")
        return

    try:
        if not svc.has_users():
            user = svc.bootstrap_admin(username, password, display_name)
            print(f"[seed_admin] 首个管理员 '{username}' 创建成功 (id={user['id']})")
        else:
            user = svc.create_user(
                username, password, display_name=display_name,
                role="admin", created_by=None,
            )
            print(f"[seed_admin] 管理员 '{username}' 创建成功 (id={user['id']})")
    except ValueError as exc:
        print(f"[seed_admin] 未创建: {exc}")
        print("  提示：该用户名可能已存在（密码不同）。改密码请在管理后台操作，或换用户名。")
        sys.exit(1)

    print(f"  写入库: {svc.db_path}")
    print("  现在即可用该账号登录（网页登录 或 POST /api/auth/login）。")


if __name__ == "__main__":
    main()
