# gov-wikidata-match

Finds reciprocal links between [GOV](https://gov.genealogy.net) building objects and [Wikidata](https://www.wikidata.org) items by matching coordinates (≤50 m) and name similarity.

## How it works

**Fetch stage** — grid-sweeps GOV's bounding-box API over the target region, collects all building objects that have no Wikidata external reference, then queries the Wikidata SPARQL endpoint for items within 50 m of each building's coordinates. Candidates are filtered to building types only (using `wd_building_types.json`) and written to a raw JSON file.

**Score stage** — loads the raw candidates and scores each GOV↔Wikidata pair using a sentence embedding model (`paraphrase-multilingual-mpnet-base-v2`). The comparison strings are `"{name} ({GOV type})"` on the GOV side and `"{label} ({Wikidata building-type labels})"` on the Wikidata side. Results are sorted by score and written to a scored JSON file.

**Review** — `review_candidates.html` is a self-contained browser tool (no server needed). Open it locally, load a scored JSON via the file picker, and browse results in a sortable table. GOV objects with multiple candidates show a count badge; clicking any row opens the GOV or Wikidata page. A score slider filters out low-confidence matches.

## Setup

```bash
pip install sentence-transformers requests
```

The Wikidata building-type QID list (`wd_building_types.json`) is committed to the repo. To regenerate it from a [Domus](https://github.com/dstraub/domus) checkout:

```bash
python extract_building_types.py path/to/domus/src/services/building-types.ts
```

## Usage

```bash
# Fetch candidates for a bounding box (Bavaria example)
python gov_wikidata_match.py fetch \
  --lat-min 47.2 --lat-max 50.6 \
  --lon-min 9.9  --lon-max 13.8 \
  --output data/bavaria_raw.json

# Score the raw candidates
python gov_wikidata_match.py score data/bavaria_raw.json \
  --output data/bavaria_scored.json

# Smoke test (small Berlin bbox, asserts one known match)
python gov_wikidata_match.py --smoke
```

# Review results

Open `review_candidates.html` directly in a browser (no server needed), use the file picker to load a scored JSON, and review matches in a sortable table with links to GOV and Wikidata.

## Output format

Both raw and scored files are JSON arrays. Each entry:

```json
{
  "gov_id": "GUNSEFJN58TL",
  "gov_names": ["Güntersdorf (St. Josef)"],
  "gov_types": [26],
  "gov_lat": 48.4816,
  "gov_lon": 11.6122,
  "candidates": [
    {
      "qid": "Q41244449",
      "label": "Katholische Pfarrkirche Sankt Joseph",
      "type_qids": ["Q16970"],
      "type_labels": ["Kirchengebäude"],
      "lat": 48.48172,
      "lon": 11.61238,
      "distance_m": 18.8,
      "score": 0.734
    }
  ]
}
```

The scored file adds `score` (cosine similarity, 0–1) and sorts candidates by score descending. Raw files omit `score` and sort by distance.

## GOV write access

GOV has a separate SOAP write API (`ChangeService`, WSDL at `https://gov.genealogy.net/services/ChangeService?wsdl`) with operations `saveObject`, `saveSource`, `merge`. None of this is implemented yet — this section documents what's needed when it is, so the SOAP details don't have to be rediscovered.

- **Auth**: every write operation takes `username` + `password` as trailing parameters (no separate API-key field exists in the schema). Credentials live in `.env` (gitignored) as `GOV_USERNAME` and `GOV_API_KEY`; `GOV_API_KEY` is used as the `password` value.
- **Transport**: RPC/literal SOAP (`soap:binding style="rpc"`), `POST` to `https://gov.genealogy.net/services/ChangeService` with `Content-Type: text/xml; charset=utf-8` and an empty `SOAPAction` header.
- **`object` element field order** (per the WSDL's `tns:object` complexType — SOAP RPC/literal is order-sensitive): `position`, `external-reference`, `url`, `name`, `type`, `population`, `postal-code`, `w-number`, `denomination`, `municipal-id`, `area`, `households`, `buildings`, `part-of`, `located-in`, `represents`, `note`. The `id` and `last-modification` are attributes on the `object` element itself (`GovItem` base type).
- **Read counterpart**: `GET https://gov.genealogy.net/api/getObject?itemId={id}` returns the current object as JSON — use this to build the full `object` XML before calling `saveObject` (the write API expects the complete object, not a partial patch).
