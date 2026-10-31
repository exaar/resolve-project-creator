"""Fail if any tracked file contains a string from a private deny-list.

Usage: python scripts/privacy_scan.py <denylist.txt>
The deny-list (one string per line) is kept OUTSIDE the repo.
"""
import subprocess
import sys
from pathlib import Path

deny = [l.strip() for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l.strip()]
files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True, check=True).stdout.split("\n")
hits = 0
for f in filter(None, files):
    try:
        text = Path(f).read_bytes().decode("utf-8", errors="ignore").lower()
    except OSError:
        continue
    for d in deny:
        if d.lower() in text:
            print(f"{f}: contains a private string (#{deny.index(d)})")
            hits += 1
log = subprocess.run(["git", "log", "--all", "--format=%an %ae %cn %ce %B"], capture_output=True, text=True).stdout.lower()
for d in deny:
    if d.lower() in log:
        print(f"git history: contains a private string (#{deny.index(d)})")
        hits += 1
print("clean" if not hits else f"{hits} hit(s)")
sys.exit(1 if hits else 0)
