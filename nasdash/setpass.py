"""Set the dashboard login: python -m nasdash.setpass [--config PATH] [--user NAME]"""

import argparse
import getpass
import os
from pathlib import Path

from .config import Config, hash_password

parser = argparse.ArgumentParser()
parser.add_argument("--config", default=os.environ.get("NASDASH_CONFIG", "/etc/nasdash/config.json"))
parser.add_argument("--user", default="admin")
parser.add_argument("--disable", action="store_true", help="remove the password (no login)")
args = parser.parse_args()

cfg = Config(Path(args.config))
if args.disable:
    cfg.update(lambda d: d["auth"].update(hash=""), keep_disk_auth=False)
    print("Login disabled.")
else:
    pw = getpass.getpass("New dashboard password: ")
    if pw != getpass.getpass("Repeat: ") or not pw:
        raise SystemExit("Passwords don't match or are empty.")
    cfg.update(lambda d: d["auth"].update(user=args.user, hash=hash_password(pw)), keep_disk_auth=False)
    print(f"Password set for user '{args.user}'. It takes effect right away.")
