"""`proof` command line."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*related API features are not available")


def _order(s: str | None):
    return [int(x) for x in s.split(",")] if s else None


def cmd_analyze(a):
    from .pipeline import analyze

    job_dir = analyze(a.pdfs, Path(a.out), profile=a.profile, job_id=a.job_id, device=a.device,
                      order=_order(a.order))
    findings = json.loads((job_dir / "findings.json").read_text())
    for f in findings:
        pages = f"p{','.join(map(str, f['pages']))}" if f["pages"] else "doc"
        print(f"{f['severity']:5}  {f['rule']:26} {pages:8} {f['detail']}")
    counts = {s: sum(f["severity"] == s for f in findings) for s in ("error", "warn", "info")}
    print(f"\n{job_dir}  errors={counts['error']} warnings={counts['warn']} info={counts['info']}")
    return 1 if counts["error"] else 0


def cmd_merge(a):
    from .ingest import merge_pdfs

    info = merge_pdfs(a.pdfs, Path(a.out), order=_order(a.order), output_intent=_intent(a.profile))
    print(json.dumps(info, indent=1))
    return 0


def _intent(profile):
    from .cms import get_press_profile

    p = get_press_profile(profile)
    return {"identifier": p.output_condition, "info": p.description, "profile_path": str(p.path())}


def cmd_profiles(a):
    from .cms import PROFILE_DIR, list_press_profiles, load_manifest

    if a.action == "list":
        default = load_manifest()["default"]
        for p in list_press_profiles():
            try:
                path = p.path()
            except FileNotFoundError:
                path = "MISSING (run scripts/fetch_profiles.py)"
            print(f"{'*' if p.name == default else ' '} {p.name:12} TAC {p.tac:g}%  {p.description}  [{path}]")
        return 0
    manifest = load_manifest()
    dst = PROFILE_DIR / "icc" / Path(a.file).name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(a.file, dst)
    entry = {"name": a.name, "file": dst.name, "description": a.description or a.name,
             "output_condition": a.condition or a.name, "tac": a.tac,
             "paper_lab": [float(x) for x in a.paper_lab.split(",")] if a.paper_lab else None, "aliases": []}
    manifest["press"] = [e for e in manifest["press"] if e["name"] != a.name] + [entry]
    (PROFILE_DIR / "profiles.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"added {a.name}")
    return 0


def _not_yet(phase):
    def run(_a):
        print(f"not implemented yet (spec phase {phase})", file=sys.stderr)
        return 2
    return run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="proof", description="Voom proofing engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="inventory + findings")
    an.add_argument("pdfs", nargs="+", type=Path)
    an.add_argument("--profile", default=None, help="press condition (default from profiles.json)")
    an.add_argument("--out", default="jobs")
    an.add_argument("--job-id")
    an.add_argument("--device", help="device id from profiles/devices.json")
    an.add_argument("--order", help="page order when several PDFs are merged, e.g. 1,2,10,3")
    an.set_defaults(fn=cmd_analyze)

    for name, phase in (("render", 2), ("run", 2)):
        p = sub.add_parser(name, help=f"(phase {phase})")
        p.add_argument("args", nargs="*")
        p.set_defaults(fn=_not_yet(phase))

    mg = sub.add_parser("merge", help="engine-controlled merge")
    mg.add_argument("pdfs", nargs="+", type=Path)
    mg.add_argument("--order")
    mg.add_argument("--out", required=True)
    mg.add_argument("--profile", default=None)
    mg.set_defaults(fn=cmd_merge)

    pr = sub.add_parser("profiles", help="list or add press profiles")
    psub = pr.add_subparsers(dest="action", required=True)
    psub.add_parser("list")
    add = psub.add_parser("add")
    add.add_argument("name")
    add.add_argument("file")
    add.add_argument("--tac", type=float, required=True)
    add.add_argument("--paper-lab")
    add.add_argument("--description")
    add.add_argument("--condition", help="OutputConditionIdentifier this profile represents")
    pr.set_defaults(fn=cmd_profiles)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
