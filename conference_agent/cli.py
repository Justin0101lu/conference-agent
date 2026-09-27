"""CLI: python -m conference_agent.cli attendees.csv --config config.yaml --out results/"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .pipeline import load_config, parse_attendees, run


def main() -> None:
    ap = argparse.ArgumentParser(description="Conference Agent — attendee list -> meetings")
    ap.add_argument("csv", help="attendee CSV (name/title/company columns)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default="results")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-dossier", action="store_true")
    a = ap.parse_args()

    cfg = load_config(a.config)
    with open(a.csv, newline="") as f:
        attendees = parse_attendees(csv.DictReader(f))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def prog(done, total, rec):
        print(f"[{done}/{total}] {'ICP ' if rec['icp_fit'] else 'skip'} {rec['name']} @ {rec['company']}")

    results = run(attendees, cfg, cache_path=str(out / "cache.json"), workers=a.workers,
                  want_dossier=not a.no_dossier, progress=prog)
    fit = [r for r in results if r["icp_fit"]]
    with open(out / "outreach.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "title", "company", "company_description", "message"])
        w.writeheader()
        for r in fit:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
    with open(out / "briefs.md", "w") as f:
        for r in fit:
            f.write(f"# {r['name']} — {r.get('title','')} @ {r['company']}\n\n{r['dossier']}\n\n---\n\n")
    json.dump(results, open(out / "results.json", "w"), indent=1)
    print(f"\n{len(fit)} ICP fits / {len(results)} attendees -> {out}/outreach.csv, briefs.md, results.json")


if __name__ == "__main__":
    main()
