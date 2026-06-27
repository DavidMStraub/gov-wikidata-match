#!/usr/bin/env python3
"""
Extract Wikidata building-type QIDs from the Domus building-types.ts source
and write them to wd_building_types.json for use by gov_wikidata_match.py.

Usage:
    python extract_building_types.py path/to/building-types.ts
"""

import re
import json
import sys

if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} path/to/building-types.ts")
    sys.exit(1)

ts_path = sys.argv[1]
out_path = "wd_building_types.json"

with open(ts_path) as fh:
    qids = sorted(set(re.findall(r"'(Q\d+)'", fh.read())))

with open(out_path, "w") as fh:
    json.dump(qids, fh, indent=2)

print(f"Wrote {len(qids)} QIDs to {out_path}")
