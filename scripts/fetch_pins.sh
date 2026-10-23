#!/bin/sh
# Clone the pinned pan/tilt sim and firmware checkouts. CI does not need them.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
pins="$root/third_party/PINS.json"
python3 - "$pins" "$root/third_party" << 'PY'
import json, subprocess, sys
from pathlib import Path
pins = json.loads(Path(sys.argv[1]).read_text())
dest_root = Path(sys.argv[2])
dest_root.mkdir(parents=True, exist_ok=True)
for name, spec in pins.items():
    dest = dest_root / name.replace("_", "-")
    if not dest.exists():
        subprocess.check_call(["git", "clone", "--filter=blob:none", spec["url"], str(dest)])
    subprocess.check_call(["git", "fetch", "--depth", "1", "origin", spec["sha"]], cwd=dest)
    subprocess.check_call(["git", "checkout", "--detach", spec["sha"]], cwd=dest)
    print(f"{name} {spec['sha']}")
PY
