"""添加或更新服务器 Profile。用法：profile_add.py <名称> [--api-base=URL] [--token=TOKEN] [--project=项目键]"""

from __future__ import annotations

import argparse
import sys

from profile import save_profile, list_profiles


def main() -> None:
    parser = argparse.ArgumentParser(description="Add/update an aiticket server profile")
    parser.add_argument("name", help="Profile 名称（如 'company-a'）")
    parser.add_argument("--api-base", help="服务器地址（如 https://aiticket.yourteam.com）")
    parser.add_argument("--token", help="账号设置 → Skill Token 处生成的 bearer token")
    parser.add_argument("--project", default="", help="默认项目键（如 EXAMPLE）")
    args = parser.parse_args()

    api_base = args.api_base
    if not api_base:
        api_base = input("Server URL (e.g. https://aiticket.yourteam.com): ").strip()
    if not api_base:
        print("ERROR: api_base is required.")
        sys.exit(1)

    token = args.token
    if not token:
        token = input("Skill token (from 账号设置 → Skill Token → 生成): ").strip()
    if not token:
        print("ERROR: token is required.")
        sys.exit(1)

    project = args.project
    if not project:
        project = input("Default project key (press Enter to skip): ").strip()

    # Quick connectivity check
    try:
        import requests
        r = requests.get(
            f"{api_base.rstrip('/')}/api/instance/config",
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        if r.status_code == 401:
            print("ERROR: Token rejected (401). Please verify your Skill token.")
            sys.exit(1)
        if not r.ok:
            print(f"WARNING: Server responded {r.status_code} — saving anyway.")
        else:
            print(f"✓ Connected to {api_base}")
    except Exception as e:
        print(f"WARNING: Could not reach server ({e}) — saving anyway.")

    save_profile(args.name, api_base, token, project)
    print(f"✓ Profile '{args.name}' saved.")

    profiles = list_profiles()
    if len(profiles) == 1:
        print(f"  (Set as default since it's the only profile)")


if __name__ == "__main__":
    main()
