"""删除指定服务器 Profile。用法：profile_remove.py <名称>"""

import argparse
from profile import delete_profile


def main() -> None:
    parser = argparse.ArgumentParser(description="删除 aiticket Profile")
    parser.add_argument("name", help="要删除的 Profile 名称")
    args = parser.parse_args()
    delete_profile(args.name)
    print(f"✓ Profile '{args.name}' removed.")


if __name__ == "__main__":
    main()
