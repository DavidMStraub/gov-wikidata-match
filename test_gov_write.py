"""Unit tests for the pure data transformations in gov_write.py.

Network calls (get_gov_object, save_gov_object) and pywikibot calls
(_wikidata_site, link_wikidata_to_gov's write path) are not unit-tested here
— mocking them would just reimplement the function bodies as assertions on
call args. They're exercised manually via write_matches.py --dry-run against
the real GOV/Wikidata APIs instead. wikidata_gov_ids_bulk's batching logic
*is* tested below by stubbing only the HTTP boundary (_wbgetentities_claims).
"""

import json

import gov_write
from gov_write import (
    Candidate,
    GovObject,
    Note,
    Position,
    Property,
    Relation,
    ScoredEntry,
    SourceRef,
    Time,
    Timespan,
    gov_object_to_xml,
    gov_wikidata_qid,
    iter_selected,
    load_scored_entries,
    parse_gov_object,
    wikidata_gov_ids_bulk,
)

# Real response from GET /api/getObject?itemId=DOMHENJO30BS (Aachener Dom),
# which already has a wikidata external-reference.
AACHENER_DOM_JSON = {
    "id": "DOMHENJO30BS",
    "lastModification": "2026-01-07T12:28:35.000+00:00",
    "position": {"lon": 6.0841, "lat": 50.77481, "height": 167, "type": "p"},
    "externalReference": [{"value": "wikidata:Q5908"}],
    "url": [
        {"value": "http://www.aachendom.de"},
        {"value": "http://whc.unesco.org/en/list/3"},
    ],
    "name": [{"lang": "deu", "value": "Aachener Dom (Maria Himmelfahrt)"}],
    "type": [{"value": "26"}],
    "denomination": [{"value": "rk"}],
    "locatedIn": [{"ref": "object_1051906"}],
    "represents": [{"ref": "object_185984"}],
    "note": [{"text": "Kathedralkirche. Weltkulturerbe", "lang": "deu"}],
}


def test_parse_gov_object_basic_fields():
    obj = parse_gov_object(AACHENER_DOM_JSON)
    assert obj.id == "DOMHENJO30BS"
    assert obj.last_modification == "2026-01-07T12:28:35.000+00:00"
    assert obj.position == Position(lon=6.0841, lat=50.77481, height=167, type="p")
    assert obj.external_reference == [Property(value="wikidata:Q5908")]
    assert obj.url == [
        Property(value="http://www.aachendom.de"),
        Property(value="http://whc.unesco.org/en/list/3"),
    ]
    assert obj.name == [Property(value="Aachener Dom (Maria Himmelfahrt)", lang="deu")]
    assert obj.type == [Property(value="26")]
    assert obj.denomination == [Property(value="rk")]
    assert obj.located_in == [Relation(ref="object_1051906")]
    assert obj.represents == [Relation(ref="object_185984")]
    assert obj.note == [Note(text="Kathedralkirche. Weltkulturerbe", lang="deu")]
    # fields absent from the JSON default to empty
    assert obj.population == []
    assert obj.part_of == []


def test_parse_gov_object_handles_nested_timespan_and_source():
    raw = {
        "id": "X1",
        "name": [
            {
                "value": "Beispiel",
                "beginYear": 1800,
                "endYear": 1900,
                "timespan": {
                    "begin": {"jd": 2378497, "precision": 1},
                    "end": {"jd": 2415021, "precision": 1},
                },
                "source": [{"ref": "source_123", "page": "12", "note": "see there"}],
            }
        ],
    }
    obj = parse_gov_object(raw)
    prop = obj.name[0]
    assert prop.begin_year == 1800
    assert prop.end_year == 1900
    assert prop.timespan == Timespan(begin=Time(jd=2378497, precision=1), end=Time(jd=2415021, precision=1))
    assert prop.source == [SourceRef(ref="source_123", page="12", note="see there")]


def test_gov_object_to_xml_round_trips_all_fields():
    obj = parse_gov_object(AACHENER_DOM_JSON)
    xml = gov_object_to_xml(obj)

    assert xml.startswith('<object xmlns="http://gov.genealogy.net/data" id="DOMHENJO30BS"')
    assert 'last-modification="2026-01-07T12:28:35.000+00:00"' in xml
    assert '<position lon="6.0841" lat="50.77481" height="167" type="p"/>' in xml
    assert '<external-reference value="wikidata:Q5908"/>' in xml
    assert '<url value="http://www.aachendom.de"/>' in xml
    assert '<name lang="deu" value="Aachener Dom (Maria Himmelfahrt)"/>' in xml
    assert '<type value="26"/>' in xml
    assert '<denomination value="rk"/>' in xml
    assert '<located-in ref="object_1051906"/>' in xml
    assert '<represents ref="object_185984"/>' in xml
    assert '<note lang="deu"><text>Kathedralkirche. Weltkulturerbe</text></note>' in xml


def test_gov_object_to_xml_field_order_matches_wsdl():
    # SOAP RPC/literal is order-sensitive: position, external-reference, url,
    # name, type, ..., located-in, represents, note (see ChangeService WSDL).
    obj = parse_gov_object(AACHENER_DOM_JSON)
    xml = gov_object_to_xml(obj)
    tags_in_order = [
        "<position", "<external-reference", "<url", "<name", "<type",
        "<denomination", "<located-in", "<represents", "<note",
    ]
    positions = [xml.index(tag) for tag in tags_in_order]
    assert positions == sorted(positions)


def test_gov_object_to_xml_escapes_special_characters():
    obj = GovObject(id="X1", name=[Property(value='A & B "quoted" <tag>')])
    xml = gov_object_to_xml(obj)
    assert "A &amp; B" in xml
    assert "&lt;tag&gt;" in xml
    assert "<tag>" not in xml.split("name")[1]  # raw markup must not leak into the value


def test_gov_wikidata_qid_extracts_existing_link():
    obj = parse_gov_object(AACHENER_DOM_JSON)
    assert gov_wikidata_qid(obj) == "Q5908"


def test_gov_wikidata_qid_none_when_absent():
    obj = GovObject(id="X1", external_reference=[Property(value="geonames:123")])
    assert gov_wikidata_qid(obj) is None


def test_gov_wikidata_qid_none_when_no_external_references():
    obj = GovObject(id="X1")
    assert gov_wikidata_qid(obj) is None


# ---------------------------------------------------------------------------
# Scored-candidates JSON parsing
# ---------------------------------------------------------------------------

SCORED_JSON = [
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
                "score": 0.94,
            }
        ],
    }
]


def test_load_scored_entries_parses_into_dataclasses(tmp_path):
    path = tmp_path / "scored.json"
    path.write_text(json.dumps(SCORED_JSON), encoding="utf-8")

    entries = load_scored_entries(str(path))

    assert entries == [
        ScoredEntry(
            gov_id="GUNSEFJN58TL",
            gov_names=["Güntersdorf (St. Josef)"],
            gov_types=[26],
            gov_lat=48.4816,
            gov_lon=11.6122,
            candidates=[
                Candidate(
                    qid="Q41244449",
                    label="Katholische Pfarrkirche Sankt Joseph",
                    type_qids=["Q16970"],
                    type_labels=["Kirchengebäude"],
                    lat=48.48172,
                    lon=11.61238,
                    distance_m=18.8,
                    score=0.94,
                )
            ],
        )
    ]


def test_iter_selected_yields_only_passing_pairs():
    good = ScoredEntry(
        gov_id="G1", gov_names=["x"], gov_types=[26], gov_lat=0.0, gov_lon=0.0,
        candidates=[Candidate(qid="Q1", label="x", type_qids=[], type_labels=[],
                               lat=0.0, lon=0.0, distance_m=10, score=0.9)],
    )
    bad = ScoredEntry(
        gov_id="G2", gov_names=["x"], gov_types=[26], gov_lat=0.0, gov_lon=0.0,
        candidates=[Candidate(qid="Q2", label="x", type_qids=[], type_labels=[],
                               lat=0.0, lon=0.0, distance_m=10, score=0.1)],
    )

    def passes(entry, candidate):
        return candidate.score >= 0.5

    result = list(iter_selected([good, bad], passes))

    assert result == [(good, good.candidates[0])]


# ---------------------------------------------------------------------------
# wikidata_gov_ids_bulk batching — stub only the HTTP boundary
# ---------------------------------------------------------------------------

def test_wikidata_gov_ids_bulk_parses_present_and_absent_claims(monkeypatch):
    def fake_wbgetentities_claims(ids_batch):
        assert ids_batch == ["Q5908", "Q1"]
        return {
            "entities": {
                "Q5908": {"claims": {"P2503": [{"mainsnak": {"datavalue": {"value": "DOMHENJO30BS"}}}]}},
                "Q1": {"claims": {}},
            }
        }

    monkeypatch.setattr(gov_write, "_wbgetentities_claims", fake_wbgetentities_claims)

    result = wikidata_gov_ids_bulk(["Q5908", "Q1"])

    assert result == {"Q5908": "DOMHENJO30BS", "Q1": None}


def test_wikidata_gov_ids_bulk_batches_in_groups_of_50_and_dedupes(monkeypatch):
    seen_batches = []

    def fake_wbgetentities_claims(ids_batch):
        seen_batches.append(list(ids_batch))
        return {"entities": {qid: {"claims": {}} for qid in ids_batch}}

    monkeypatch.setattr(gov_write, "_wbgetentities_claims", fake_wbgetentities_claims)
    monkeypatch.setattr(gov_write.time, "sleep", lambda _: None)

    qids = [f"Q{i}" for i in range(60)] + ["Q0"]  # Q0 is a duplicate of the first id
    result = wikidata_gov_ids_bulk(qids)

    assert len(seen_batches) == 2
    assert len(seen_batches[0]) == 50
    assert len(seen_batches[1]) == 10
    assert set(result) == {f"Q{i}" for i in range(60)}


# ---------------------------------------------------------------------------
# run_writes skip/limit slicing — stub the two network-touching calls it
# delegates to (already covered by their own dedicated tests/manual runs)
# so this test only exercises the selection/slicing logic.
# ---------------------------------------------------------------------------

def test_run_writes_skip_and_limit_slice_the_filtered_matches(monkeypatch):
    monkeypatch.setattr(gov_write, "wikidata_gov_ids_bulk", lambda qids: {q: None for q in qids})
    monkeypatch.setattr(gov_write, "link_gov_to_wikidata", lambda gov_id, qid, dry_run: "dry-run")
    monkeypatch.setattr(gov_write, "link_wikidata_to_gov",
                         lambda qid, gov_id, dry_run, existing, put_throttle: "dry-run")

    entries = [
        ScoredEntry(gov_id=f"G{i}", gov_names=["x"], gov_types=[26], gov_lat=0.0, gov_lon=0.0,
                    candidates=[Candidate(qid=f"Q{i}", label="x", type_qids=[], type_labels=[],
                                           lat=0.0, lon=0.0, distance_m=0, score=1.0)])
        for i in range(10)
    ]

    results_all = gov_write.run_writes(entries, lambda e, c: True, dry_run=True)
    assert len(results_all) == 10
    assert all(r.wd_status == "dry-run" for r in results_all)

    results_batch = gov_write.run_writes(entries, lambda e, c: True, dry_run=True, skip=3, limit=4)
    assert len(results_batch) == 4
    assert [r.entry.gov_id for r in results_batch] == ["G3", "G4", "G5", "G6"]


def test_run_writes_isolates_a_failing_match_and_continues(monkeypatch):
    monkeypatch.setattr(gov_write, "wikidata_gov_ids_bulk", lambda qids: {q: None for q in qids})

    def fake_link_gov_to_wikidata(gov_id, qid, dry_run):
        if gov_id == "G1":
            raise RuntimeError("GOV is down")
        return "linked"

    monkeypatch.setattr(gov_write, "link_gov_to_wikidata", fake_link_gov_to_wikidata)
    monkeypatch.setattr(gov_write, "link_wikidata_to_gov",
                         lambda qid, gov_id, dry_run, existing, put_throttle: "linked")

    entries = [
        ScoredEntry(gov_id=f"G{i}", gov_names=["x"], gov_types=[26], gov_lat=0.0, gov_lon=0.0,
                    candidates=[Candidate(qid=f"Q{i}", label="x", type_qids=[], type_labels=[],
                                           lat=0.0, lon=0.0, distance_m=0, score=1.0)])
        for i in range(3)
    ]

    results = gov_write.run_writes(entries, lambda e, c: True, dry_run=False)

    assert [r.entry.gov_id for r in results] == ["G0", "G1", "G2"]
    assert results[0].wd_status == "linked"
    assert results[1].gov_status == "error"
    assert "GOV is down" in results[1].wd_status
    assert results[2].wd_status == "linked"

