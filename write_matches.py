#!/usr/bin/env /home/david/.venv/default/bin/python
"""Write hand-picked GOV<->Wikidata links from a scored candidates JSON.

Links are written on both sides: GOV gets an external-reference to the
Wikidata QID, Wikidata gets a P2503 claim with the GOV id. Both are no-ops
if already linked. Defaults to --dry-run; pass --live to actually write.

Usage:
    python write_matches.py data/bavaria_scored.json --live
    python write_matches.py data/bavaria_scored.json --object-id MUNHIMJN58SC --live
    python write_matches.py data/bavaria_scored.json --skip 0    --limit 500 --live
    python write_matches.py data/bavaria_scored.json --skip 500  --limit 500 --live
"""

import argparse
import datetime
from pathlib import Path

from gov_write import Candidate, ScoredEntry, load_scored_entries, run_writes


def passes_filters(entry: ScoredEntry, candidate: Candidate) -> bool:
    """Which GOV<->Wikidata matches are eligible for writing. Edit directly —
    filters are code, not CLI flags."""
    if candidate.score < 0.9:
        return False
    if candidate.distance_m > 25.0:
        return False
    if len(entry.candidates) > 1:
        return False
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scored_json")
    ap.add_argument("--object-id", help="Only process this single gov_id (for testing)")
    ap.add_argument("--skip", type=int, default=0,
                     help="Skip the first N filtered matches (for batching)")
    ap.add_argument("--limit", type=int, default=None,
                     help="Process at most N filtered matches (for batching)")
    ap.add_argument("--live", action="store_true", help="Actually write (default is dry-run)")
    args = ap.parse_args()

    dry_run = not args.live
    if dry_run:
        print("=== DRY RUN (pass --live to actually write) ===")

    entries = load_scored_entries(args.scored_json)
    results = run_writes(entries, passes_filters, object_id=args.object_id, dry_run=dry_run,
                          skip=args.skip, limit=args.limit)
    print(f"\n{len(results)} match(es) processed.")

    newly_linked = [r for r in results if r.wd_status == "linked"]
    if newly_linked:
        urls = [f"https://www.wikidata.org/wiki/{r.candidate.qid}" for r in newly_linked]
        print(f"\n{len(urls)} Wikidata item(s) newly linked this run:")
        for url in urls:
            print(url)

        out_path = Path(args.scored_json).with_name(
            f"linked_{datetime.datetime.now():%Y%m%dT%H%M%S}.txt")
        out_path.write_text("\n".join(urls) + "\n", encoding="utf-8")
        print(f"\nSaved to {out_path}")
