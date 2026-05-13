"""列出所有已配置的服务器 Profile。"""

from profile import list_profiles


def main() -> None:
    profiles = list_profiles()
    if not profiles:
        print("No profiles configured. Run /aiticket-profile-add <name> to add one.")
        return
    print(f"{'NAME':<20} {'DEFAULT':<8} {'API BASE':<40} {'PROJECT':<12} TOKEN")
    print("-" * 100)
    for p in profiles:
        marker = "✓" if p["is_default"] else ""
        print(f"{p['name']:<20} {marker:<8} {p['api_base']:<40} {p['default_project']:<12} {p['token_masked']}")


if __name__ == "__main__":
    main()
