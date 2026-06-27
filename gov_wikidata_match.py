#!/usr/bin/env /home/david/.venv/default/bin/python
"""Match GOV building objects to Wikidata items by coordinates (≤50 m)."""

import math
import re
import time
import json
import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

BUILDING_TYPES: frozenset[int] = frozenset([
    8,   # Burg
    17,  # Gebäude
    21,  # Gut
    24,  # Hof
    26,  # Kirche
    30,  # Kloster
    64,  # Vorwerk
    87,  # Mühle
    102, # Forsthaus
    111, # Schloss
    118, # Bahnhof
    119, # Haltestelle
    124, # Abtei
    166, # Ruine
    193, # Alm
    244, # Nebenkirche
    245, # Kapelle
])

GOV_API = "https://gov.genealogy.net/api"
SPARQL  = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "gov-wikidata-match/1.0"}

# Wikidata QIDs accepted as building types — loaded from Domus building-types.ts
WD_BUILDING_TYPES: frozenset[str] = frozenset()


def load_wd_building_types(json_path: str = "wd_building_types.json") -> None:
    global WD_BUILDING_TYPES
    with open(json_path) as fh:
        WD_BUILDING_TYPES = frozenset(json.load(fh))
    print(f"Loaded {len(WD_BUILDING_TYPES)} Wikidata building type QIDs")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GovBuilding:
    id: str
    names: list[str]
    types: list[int]
    lat: float
    lon: float
    ext_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

_embed_model = None

_MODEL_NAME = "paraphrase-multilingual-mpnet-base-v2"


def _get_model():  # type: ignore[return]
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(_MODEL_NAME)
    return _embed_model


GOV_TYPE_LABEL: dict[int, str] = {
    8: "Burg", 17: "Gebäude", 21: "Gut", 24: "Hof", 26: "Kirche",
    30: "Kloster", 64: "Vorwerk", 87: "Mühle", 102: "Forsthaus",
    111: "Schloss", 118: "Bahnhof", 119: "Haltestelle", 124: "Abtei",
    166: "Ruine", 193: "Alm", 244: "Nebenkirche", 245: "Kapelle",
}


def _gov_texts(gov: GovBuilding) -> list[str]:
    type_str = " ".join(GOV_TYPE_LABEL[t] for t in gov.types if t in GOV_TYPE_LABEL)
    return [f"{n.replace('/', ' ')} ({type_str})".strip() for n in gov.names]


def _wd_text(candidate: dict) -> str:
    type_str = " ".join(candidate.get("type_labels", []))
    return f"{candidate['label']} ({type_str})".strip()


def score_candidate(
    gov: GovBuilding,
    candidate: dict,
    gov_embs: np.ndarray | None = None,
) -> float:
    """Cosine similarity between GOV query embedding and WD passage embedding."""
    model = _get_model()
    wd_emb = model.encode([_wd_text(candidate)],
                          convert_to_numpy=True, normalize_embeddings=True)[0]
    if gov_embs is None:
        gov_embs = model.encode(_gov_texts(gov),
                                convert_to_numpy=True, normalize_embeddings=True)
    return round(float(np.max(gov_embs @ wd_emb)), 3)


# ---------------------------------------------------------------------------
# Step 1: Fetch all GOV buildings without a wikidata ref
# ---------------------------------------------------------------------------

def _fetch_bbox(lat0: float, lat1: float, lon0: float, lon1: float) -> list[GovBuilding]:
    """Return GOV building objects in bbox that have no wikidata externalReference."""
    for attempt in range(3):
        try:
            r = requests.get(
                f"{GOV_API}/searchByBoundingBox",
                params={"latitude0": lat0, "latitude1": lat1,
                        "longitude0": lon0, "longitude1": lon1},
                timeout=60,
                headers=HEADERS,
            )
            r.raise_for_status()
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            wait = 10 * (attempt + 1)
            print(f"  [{lat0:.1f},{lon0:.1f}] timeout, retrying in {wait}s…")
            time.sleep(wait)

    results = []
    for obj in r.json():
        types = {int(t["value"]) for t in obj.get("type", [])}
        if not (types & BUILDING_TYPES):
            continue

        ext = [e.get("value", "") for e in obj.get("externalReference", [])]
        if any("wikidata" in v for v in ext):
            continue  # already linked

        pos = obj.get("position")
        if not pos:
            continue  # no coordinates → can't match spatially

        results.append(GovBuilding(
            id=obj["id"],
            names=[n["value"] for n in obj.get("name", [])],
            types=sorted(types & BUILDING_TYPES),
            lat=pos["lat"],
            lon=pos["lon"],
            ext_refs=ext,
        ))
    return results


def fetch_gov_buildings(
    lat_min: float = 45.0,
    lat_max: float = 57.0,
    lon_min: float = 5.0,
    lon_max: float = 25.0,
    step: float = 0.5,
    workers: int = 8,
) -> list[GovBuilding]:
    """
    Grid-sweep GOV and return all building objects without a wikidata ref.
    Default bbox covers German-speaking Europe + historical German territories.
    """
    lats = []
    lat = lat_min
    while lat < lat_max:
        lats.append(round(lat, 4))
        lat = round(lat + step, 4)
    lons = []
    lon = lon_min
    while lon < lon_max:
        lons.append(round(lon, 4))
        lon = round(lon + step, 4)

    tiles = [(la, round(la + step, 4), lo, round(lo + step, 4))
             for la in lats for lo in lons]

    seen: dict[str, GovBuilding] = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_bbox, *t): t for t in tiles}
        for fut in as_completed(futures):
            lat0, _, lon0, _ = futures[fut]
            try:
                batch = fut.result()
                new = sum(1 for b in batch if b.id not in seen)
                for b in batch:
                    seen[b.id] = b
                print(f"  [{lat0:.1f},{lon0:.1f}] {len(batch)} buildings, {new} new")
            except Exception as e:
                print(f"  [{lat0:.1f},{lon0:.1f}] ERROR {e}")

    buildings = list(seen.values())
    print(f"\nTotal GOV buildings without wikidata ref: {len(buildings)}")
    return buildings


# ---------------------------------------------------------------------------
# Wikidata fetch (tiled) + in-memory spatial match
# ---------------------------------------------------------------------------

def _sparql_get(query: str) -> requests.Response:
    """GET a SPARQL query with up to 5 retries on timeout or 429/5xx."""
    for attempt in range(5):
        try:
            r = requests.get(
                SPARQL,
                params={"query": query, "format": "json"},
                headers={**HEADERS, "Accept": "application/sparql-results+json"},
                timeout=60,
            )
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", 10 * (attempt + 1)))
                if attempt == 4:
                    r.raise_for_status()
                print(f"    SPARQL {r.status_code}, retrying in {wait}s…")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout:
            if attempt == 4:
                raise
            wait = 10 * (attempt + 1)
            print(f"    SPARQL timeout, retrying in {wait}s…")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance in metres — error is negligible at ≤50 m."""
    dlat = (lat2 - lat1) * 111_000
    dlon = (lon2 - lon1) * 111_000 * math.cos(math.radians(lat1))
    return math.sqrt(dlat * dlat + dlon * dlon)


_WD_BOX_QUERY = """\
SELECT ?item ?itemLabel ?type ?typeLabel ?coord WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({lon0} {lat0})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({lon1} {lat1})"^^geo:wktLiteral .
  }}
  ?item wdt:P31 ?type .
  FILTER NOT EXISTS {{ ?item wdt:P2503 ?gov . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "de,en" . }}
}}
LIMIT 10000
"""


def _fetch_wd_tile(lat0: float, lat1: float, lon0: float, lon1: float) -> list[dict]:
    """Fetch all Wikidata items with P625+P31 in bbox, excluding already-linked ones.
    Subdivides into quadrants if the server limit is hit."""
    query = _WD_BOX_QUERY.format(lat0=lat0, lat1=lat1, lon0=lon0, lon1=lon1)
    r = _sparql_get(query)
    rows = r.json()["results"]["bindings"]

    if len(rows) == 10_000:
        lat_mid = round((lat0 + lat1) / 2, 6)
        lon_mid = round((lon0 + lon1) / 2, 6)
        print(f"  [{lat0:.3f},{lon0:.3f}] hit limit, subdividing into quadrants…")
        by_qid: dict[str, dict] = {}
        for sub in [(lat0, lat_mid, lon0, lon_mid), (lat0, lat_mid, lon_mid, lon1),
                    (lat_mid, lat1, lon0, lon_mid), (lat_mid, lat1, lon_mid, lon1)]:
            for item in _fetch_wd_tile(*sub):
                by_qid[item["qid"]] = item
        return list(by_qid.values())

    by_qid = {}
    for row in rows:
        coord = row.get("coord", {}).get("value", "")
        m = re.search(r"Point\(([+-]?\d+\.?\d*)\s+([+-]?\d+\.?\d*)\)", coord)
        if not m:
            continue
        wlon, wlat = float(m.group(1)), float(m.group(2))
        qid = row["item"]["value"].split("/")[-1]
        type_uri = row.get("type", {}).get("value", "")
        type_qid = type_uri.split("/")[-1] if type_uri else ""
        type_label = row.get("typeLabel", {}).get("value", "")

        if qid not in by_qid:
            by_qid[qid] = {
                "qid":   qid,
                "label": row.get("itemLabel", {}).get("value", ""),
                "lat":   wlat,
                "lon":   wlon,
                "types": set(),
            }
        if type_qid:
            by_qid[qid]["types"].add((type_qid, type_label))

    for item in by_qid.values():
        item["types"] = list(item["types"])
    return list(by_qid.values())


def fetch_all_wd_buildings(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    step: float = 0.5,
    workers: int = 3,
) -> list[dict]:
    """Tile-fetch all Wikidata buildings in bbox. Returns deduplicated list."""
    lats, lons = [], []
    lat = lat_min
    while lat < lat_max:
        lats.append(round(lat, 4))
        lat = round(lat + step, 4)
    lon = lon_min
    while lon < lon_max:
        lons.append(round(lon, 4))
        lon = round(lon + step, 4)
    tiles = [(la, round(la + step, 4), lo, round(lo + step, 4))
             for la in lats for lo in lons]

    seen: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_wd_tile, *t): t for t in tiles}
        for fut in as_completed(futures):
            lat0, _, lon0, _ = futures[fut]
            try:
                items = fut.result()
                new = sum(1 for it in items if it["qid"] not in seen)
                for it in items:
                    seen[it["qid"]] = it
                print(f"  [{lat0:.1f},{lon0:.1f}] {len(items)} items, {new} new")
            except Exception as e:
                print(f"  [{lat0:.1f},{lon0:.1f}] ERROR {e}")

    result = list(seen.values())
    print(f"\nTotal Wikidata buildings: {len(result)}")
    return result


_IDX_STEP_LAT = 0.001  # ≈ 111 m per cell; 3×3 neighbourhood covers ≈ 333 m


def _build_spatial_index(wd_items: list[dict], center_lat: float) -> tuple[dict, float]:
    """Bucket WD items by fine grid cell. lon_step is adjusted for meridian convergence."""
    lon_step = _IDX_STEP_LAT / math.cos(math.radians(center_lat))
    index: dict[tuple[int, int], list[dict]] = {}
    for item in wd_items:
        lat_key = math.floor(item["lat"] / _IDX_STEP_LAT)
        lon_key = math.floor(item["lon"] / lon_step)
        index.setdefault((lat_key, lon_key), []).append(item)
    return index, lon_step


def find_candidates_in_memory(
    gov: GovBuilding,
    wd_index: dict,
    lon_step: float,
    radius_m: float = 50.0,
) -> list[dict]:
    """Find WD candidates within radius_m of gov using the spatial index."""
    lat_key = math.floor(gov.lat / _IDX_STEP_LAT)
    lon_key = math.floor(gov.lon / lon_step)

    candidates = []
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for wd in wd_index.get((lat_key + dlat, lon_key + dlon), []):
                dist = _dist_m(gov.lat, gov.lon, wd["lat"], wd["lon"])
                if dist > radius_m:
                    continue
                # type_labels contains ONLY the building-type subset so non-building co-types
                # (Bodendenkmal, Befund, …) never reach the scorer.
                building_types = [(q, l) for q, l in wd["types"] if q in WD_BUILDING_TYPES]
                if WD_BUILDING_TYPES and not building_types:
                    continue
                candidates.append({
                    "qid":         wd["qid"],
                    "label":       wd["label"],
                    "type_qids":   sorted(q for q, _ in building_types),
                    "type_labels": [l for _, l in building_types if l],
                    "lat":         wd["lat"],
                    "lon":         wd["lon"],
                    "distance_m":  round(dist, 1),
                })

    return sorted(candidates, key=lambda x: x["distance_m"])


# ---------------------------------------------------------------------------
# Stage 1: Match GOV buildings against pre-fetched WD index
# ---------------------------------------------------------------------------

def fetch_all_candidates(
    buildings: list[GovBuilding],
    wd_index: dict,
    lon_step: float,
    radius_m: float = 50.0,
    output_path: str = "gov_candidates_raw.json",
) -> list[dict]:
    """Match GOV buildings against WD spatial index. Writes raw results (no scores)."""
    results = []
    for i, gov in enumerate(buildings):
        cands = find_candidates_in_memory(gov, wd_index, lon_step, radius_m=radius_m)
        if not cands:
            continue

        entry = {
            "gov_id":     gov.id,
            "gov_names":  gov.names,
            "gov_types":  gov.types,
            "gov_lat":    gov.lat,
            "gov_lon":    gov.lon,
            "candidates": cands,
        }
        results.append(entry)
        print(f"  [{i+1}/{len(buildings)}] {gov.id} {gov.names[:1]} → {len(cands)} candidate(s)")

        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(results)}/{len(buildings)} buildings have candidates → {output_path}")
    return results


# ---------------------------------------------------------------------------
# Stage 2: Score raw candidates
# ---------------------------------------------------------------------------

def score_all_candidates(
    input_path: str,
    output_path: str = "gov_candidates_scored.json",
) -> None:
    """Read raw candidates JSON, add similarity scores, write scored output."""
    with open(input_path) as f:
        entries = json.load(f)

    model = _get_model()
    results = []
    for i, entry in enumerate(entries):
        gov = GovBuilding(
            id=entry["gov_id"],
            names=entry["gov_names"],
            types=entry["gov_types"],
            lat=entry["gov_lat"],
            lon=entry["gov_lon"],
        )
        gov_embs = model.encode(
            _gov_texts(gov), convert_to_numpy=True, normalize_embeddings=True,
        )
        cands = [dict(c) for c in entry["candidates"]]
        for c in cands:
            c["score"] = score_candidate(gov, c, gov_embs)
        cands.sort(key=lambda c: c["score"], reverse=True)

        results.append({**entry, "candidates": cands})
        print(f"  [{i+1}/{len(entries)}] {entry['gov_id']} {entry['gov_names'][:1]} → "
              + ", ".join(f"{c['qid']} score={c['score']} @{c['distance_m']}m" for c in cands))

        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Scored {len(results)} entries → {output_path}")


# ---------------------------------------------------------------------------
# Smoke test:  python gov_wikidata_match.py --smoke
# Fetch:       python gov_wikidata_match.py fetch [--lat-min ...] [--output raw.json]
# Score:       python gov_wikidata_match.py score raw.json [--output scored.json]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse

    if "--smoke" in sys.argv:
        load_wd_building_types()
        lat0, lat1, lon0, lon1 = 52.49, 52.51, 13.38, 13.41
        print("=== Smoke test: small bbox around Heilig-Kreuz-Kirche, Berlin ===")
        sample = _fetch_bbox(lat0, lat1, lon0, lon1)
        print(f"GOV buildings without wikidata ref: {len(sample)}")
        for b in sample:
            print(f"  {b.id} {b.names[:1]} types={b.types} ({b.lat},{b.lon})")

        target = next((b for b in sample if b.id == "object_334790"), None)
        assert target is not None, "Expected object_334790 in bbox results"

        print(f"\n=== Wikidata candidates within 50 m of '{target.names[0]}' ===")
        wd_items = fetch_all_wd_buildings(lat0, lat1, lon0, lon1, workers=1)
        wd_index, lon_step = _build_spatial_index(wd_items, center_lat=(lat0 + lat1) / 2)
        cands = find_candidates_in_memory(target, wd_index, lon_step)
        print(f"Candidates: {len(cands)}")
        for c in cands:
            print(f"  {c['qid']} {c['type_labels']} '{c['label']}' @ {c['distance_m']} m")
        assert any(c["qid"] == "Q1594935" for c in cands), "Expected Q1594935 in candidates"
        print("✓ assertion passed")

    else:
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd", required=True)

        fp = sub.add_parser("fetch", help="Fetch GOV buildings and Wikidata candidates")
        fp.add_argument("--lat-min", type=float, default=45.0)
        fp.add_argument("--lat-max", type=float, default=57.0)
        fp.add_argument("--lon-min", type=float, default=5.0)
        fp.add_argument("--lon-max", type=float, default=25.0)
        fp.add_argument("--output", default="gov_candidates_raw.json")
        fp.add_argument("--step", type=float, default=0.5,
                        help="GOV tile size in degrees (default 0.5)")
        fp.add_argument("--wd-step", type=float, default=0.5,
                        help="Wikidata tile size in degrees (default 0.5); dense tiles auto-subdivide")
        fp.add_argument("--workers", type=int, default=8,
                        help="Parallel tile fetches (default 8)")
        fp.add_argument("--building-types", default="wd_building_types.json",
                        help="QID list JSON produced by extract_building_types.py")

        sp = sub.add_parser("score", help="Score raw candidates with sentence embeddings")
        sp.add_argument("input", help="Raw candidates JSON from the fetch stage")
        sp.add_argument("--output", default="gov_candidates_scored.json")

        args = ap.parse_args()

        if args.cmd == "fetch":
            load_wd_building_types(args.building_types)
            print("=== Fetching GOV buildings ===")
            buildings = fetch_gov_buildings(
                lat_min=args.lat_min, lat_max=args.lat_max,
                lon_min=args.lon_min, lon_max=args.lon_max,
                step=args.step, workers=args.workers,
            )
            print("\n=== Fetching Wikidata buildings ===")
            wd_items = fetch_all_wd_buildings(
                lat_min=args.lat_min, lat_max=args.lat_max,
                lon_min=args.lon_min, lon_max=args.lon_max,
                step=args.wd_step,
            )
            center_lat = (args.lat_min + args.lat_max) / 2
            wd_index, lon_step = _build_spatial_index(wd_items, center_lat)
            print("\n=== Matching candidates ===")
            fetch_all_candidates(buildings, wd_index, lon_step, output_path=args.output)

        elif args.cmd == "score":
            print(f"=== Scoring candidates from {args.input} ===")
            score_all_candidates(args.input, output_path=args.output)
