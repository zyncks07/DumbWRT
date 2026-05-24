#!/usr/bin/env python3
"""Bootstrap /etc/openwrt-monitor/auth.json with an admin password.

Run as root once at install time, or any time you want to reset the
admin credentials from the command line:

    sudo python3 /home/bulik/apps/openwrt-monitor/scripts/init_auth.py

Prompts for username and password (twice). Existing auth.json is
overwritten — for routine rotation use the Settings UI instead.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth


def main() -> int:
    print(f"Bootstrapping {auth.AUTH_PATH}")
    username = input("Username [admin]: ").strip() or "admin"

    while True:
        pw1 = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Confirm:  ")
        if pw1 != pw2:
            print("Mismatch — try again.")
            continue
        if len(pw1) < 4:
            print("Too short (min 4 chars) — try again.")
            continue
        break

    auth.bootstrap(username, pw1)
    print(f"Wrote {auth.AUTH_PATH} (mode 600).")
    print("Restart the dashboard to pick up the new secret_key:")
    print("    sudo systemctl restart openwrt-dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
