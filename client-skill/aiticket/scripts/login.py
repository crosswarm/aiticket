"""
/aiticket-login — /aiticket-profile-add 的引导式别名。
用法：python scripts/login.py [--api-base URL] [--token TOKEN] [--project 项目键]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from profile_add import main

if __name__ == "__main__":
    import sys as _sys
    # Default profile name to "default" when invoked via /aiticket-login
    if len(_sys.argv) == 1:
        _sys.argv = [_sys.argv[0], "default"]
    main()
