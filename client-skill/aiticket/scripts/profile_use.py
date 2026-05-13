"""将指定 Profile 设为默认。用法：profile_use.py <名称>"""

import argparse
from profile import set_default, list_profiles


def main() -> None:
    parser = argparse.ArgumentParser(description="设置 aiticket 默认 Profile")
    parser.add_argument("name", help="要激活的 Profile 名称")
    args = parser.parse_args()
    set_default(args.name)
    print(f"✓ Default profile set to '{args.name}'")


if __name__ == "__main__":
    main()
