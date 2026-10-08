"""Bake a keyless snapshot (plus optional hand-checked blurbs) into a single demo page.

    python demo/build.py SNAPSHOT.json [--blurbs demo/blurbs.json] --out demo/bait.html
blurbs.json: {"<slug>": {"why": "...", "receipts": [{"title","url","source"}]}}
"""
import argparse
import json
import pathlib

ap = argparse.ArgumentParser()
ap.add_argument("snapshot")
ap.add_argument("--blurbs")
ap.add_argument("--out", required=True)
a = ap.parse_args()
snap = json.load(open(a.snapshot))
if a.blurbs:
    blurbs = json.load(open(a.blurbs))
    hit = 0
    for p in snap["projects"]:
        b = blurbs.get(p["slug"])
        if b:
            hit += 1
            p["whyBlurb"] = b.get("why", "") or p.get("whyBlurb", "")
            p["receipts"] = b.get("receipts", []) or p.get("receipts", [])
    print(f"blurbs applied: {hit}/{len(blurbs)}")
tpl = (pathlib.Path(__file__).parent / "template.html").read_text()
data = json.dumps(snap, separators=(",", ":")).replace("</", "<\\/")
out = tpl.replace("__SNAPSHOT__", data)
pathlib.Path(a.out).write_text(out)
print(f"wrote {a.out} ({len(out)/1024:.0f} KB, {len(snap['projects'])} projects)")
