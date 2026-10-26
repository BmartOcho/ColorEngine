"""Download the press ICC profiles listed in profiles/profiles.json and verify SHA-256.

The profiles come from the ICC characterization registry and are not committed.
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "profiles"


def main() -> int:
    manifest = json.loads((ROOT / "profiles.json").read_text())
    (ROOT / "icc").mkdir(exist_ok=True)
    bad = 0
    for p in manifest["press"]:
        dst = ROOT / "icc" / p["file"]
        if not dst.exists() and p.get("url"):
            print(f"fetching {p['file']}")
            req = urllib.request.Request(p["url"], headers={"User-Agent": "proofing-engine/0.1"})
            with urllib.request.urlopen(req) as r:
                dst.write_bytes(r.read())
        if not dst.exists():
            print(f"missing {p['file']} (no url; add it with `proof profiles add`)")
            bad += 1
            continue
        digest = hashlib.sha256(dst.read_bytes()).hexdigest()
        ok = not p.get("sha256") or digest == p["sha256"]
        print(f"{'ok ' if ok else 'BAD'} {p['file']} {digest}")
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
