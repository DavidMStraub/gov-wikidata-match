"""Write access: link GOV objects and Wikidata items to each other.

GOV side:      ChangeService.saveObject (SOAP) — adds an
               external-reference value="wikidata:{QID}".
Wikidata side: P2503 ("GOV identifier") claim via pywikibot.

Both writes are no-ops if the link already exists.
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable
from xml.sax.saxutils import escape

import requests

GOV_API = "https://gov.genealogy.net/api"
GOV_CHANGE_SERVICE = "https://gov.genealogy.net/services/ChangeService"
WD_API = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "gov-wikidata-match/1.0"}

_UNSET = object()  # sentinel: "no precomputed value given, look it up"

GOV_P2503 = "P2503"  # Wikidata property: GOV identifier


def _load_dotenv(path: str = ".env") -> dict[str, str]:
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_ENV = _load_dotenv()
GOV_USERNAME = os.environ.get("GOV_USERNAME") or _ENV.get("GOV_USERNAME")
GOV_API_KEY = os.environ.get("GOV_API_KEY") or _ENV.get("GOV_API_KEY")
WD_USERNAME = os.environ.get("WD_USERNAME") or _ENV.get("WD_USERNAME")
WD_BOT_PASSWORD = os.environ.get("WD_BOT_PASSWORD") or _ENV.get("WD_BOT_PASSWORD")


# ---------------------------------------------------------------------------
# GOV object model — mirrors the ChangeService WSDL's tns:object complexType.
# Field order in GovObject._XML_FIELDS matches the WSDL exactly, since the
# SOAP binding is RPC/literal (order-sensitive).
# ---------------------------------------------------------------------------

@dataclass
class Time:
    jd: int
    precision: int


@dataclass
class Timespan:
    begin: Time | None = None
    end: Time | None = None


@dataclass
class SourceRef:
    ref: str | None = None
    page: str | None = None
    note: str | None = None


@dataclass
class Property:
    value: str | None = None
    lang: str | None = None
    begin_year: int | None = None
    end_year: int | None = None
    year: int | None = None
    timespan: Timespan | None = None
    source: list[SourceRef] = field(default_factory=list)


@dataclass
class Relation:
    ref: str
    begin_year: int | None = None
    end_year: int | None = None
    year: int | None = None
    timespan: Timespan | None = None
    source: list[SourceRef] = field(default_factory=list)


@dataclass
class Note:
    text: str
    lang: str | None = None
    source: list[SourceRef] = field(default_factory=list)


@dataclass
class Position:
    lon: float
    lat: float
    height: int | None = None
    type: str | None = None


@dataclass
class GovObject:
    id: str
    last_modification: str | None = None
    deprecated: str | None = None
    position: Position | None = None
    external_reference: list[Property] = field(default_factory=list)
    url: list[Property] = field(default_factory=list)
    name: list[Property] = field(default_factory=list)
    type: list[Property] = field(default_factory=list)
    population: list[Property] = field(default_factory=list)
    postal_code: list[Property] = field(default_factory=list)
    w_number: list[Property] = field(default_factory=list)
    denomination: list[Property] = field(default_factory=list)
    municipal_id: list[Property] = field(default_factory=list)
    area: list[Property] = field(default_factory=list)
    households: list[Property] = field(default_factory=list)
    buildings: list[Property] = field(default_factory=list)
    part_of: list[Relation] = field(default_factory=list)
    located_in: list[Relation] = field(default_factory=list)
    represents: list[Relation] = field(default_factory=list)
    note: list[Note] = field(default_factory=list)


# (dataclass field name, XML element name), in WSDL order, excluding `position`
# (emitted separately, first) and the GovItem attributes id/last-modification/deprecated.
_PROPERTY_FIELDS = [
    ("external_reference", "external-reference"),
    ("url", "url"),
    ("name", "name"),
    ("type", "type"),
    ("population", "population"),
    ("postal_code", "postal-code"),
    ("w_number", "w-number"),
    ("denomination", "denomination"),
    ("municipal_id", "municipal-id"),
    ("area", "area"),
    ("households", "households"),
    ("buildings", "buildings"),
]
_RELATION_FIELDS = [
    ("part_of", "part-of"),
    ("located_in", "located-in"),
    ("represents", "represents"),
]
_NOTE_FIELDS = [("note", "note")]


# ---------------------------------------------------------------------------
# JSON (GET /api/getObject) -> GovObject
# ---------------------------------------------------------------------------

def _parse_time(d: dict | None) -> Time | None:
    return Time(jd=d["jd"], precision=d["precision"]) if d else None


def _parse_timespan(d: dict | None) -> Timespan | None:
    return Timespan(begin=_parse_time(d.get("begin")), end=_parse_time(d.get("end"))) if d else None


def _parse_source_refs(items: list[dict] | None) -> list[SourceRef]:
    return [SourceRef(ref=s.get("ref"), page=s.get("page"), note=s.get("note")) for s in items or []]


def _parse_property(d: dict) -> Property:
    return Property(
        value=d.get("value"),
        lang=d.get("lang"),
        begin_year=d.get("beginYear"),
        end_year=d.get("endYear"),
        year=d.get("year"),
        timespan=_parse_timespan(d.get("timespan")),
        source=_parse_source_refs(d.get("source")),
    )


def _parse_relation(d: dict) -> Relation:
    return Relation(
        ref=d["ref"],
        begin_year=d.get("beginYear"),
        end_year=d.get("endYear"),
        year=d.get("year"),
        timespan=_parse_timespan(d.get("timespan")),
        source=_parse_source_refs(d.get("source")),
    )


def _parse_note(d: dict) -> Note:
    return Note(text=d["text"], lang=d.get("lang"), source=_parse_source_refs(d.get("source")))


def _parse_position(d: dict | None) -> Position | None:
    return Position(lon=d["lon"], lat=d["lat"], height=d.get("height"), type=d.get("type")) if d else None


def parse_gov_object(d: dict) -> GovObject:
    kwargs = {}
    for json_key, _ in _PROPERTY_FIELDS:
        camel = json_key.split("_")[0] + "".join(p.title() for p in json_key.split("_")[1:])
        kwargs[json_key] = [_parse_property(p) for p in d.get(camel, []) or []]
    for json_key, _ in _RELATION_FIELDS:
        camel = json_key.split("_")[0] + "".join(p.title() for p in json_key.split("_")[1:])
        kwargs[json_key] = [_parse_relation(r) for r in d.get(camel, []) or []]
    kwargs["note"] = [_parse_note(n) for n in d.get("note", []) or []]

    return GovObject(
        id=d["id"],
        last_modification=d.get("lastModification"),
        deprecated=d.get("deprecated"),
        position=_parse_position(d.get("position")),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# GovObject -> XML
# ---------------------------------------------------------------------------

def _source_refs_xml(sources: list[SourceRef]) -> str:
    parts = []
    for s in sources:
        attrs = f' ref="{escape(s.ref)}"' if s.ref else ""
        inner = ""
        if s.page:
            inner += f"<page>{escape(s.page)}</page>"
        if s.note:
            inner += f"<note>{escape(s.note)}</note>"
        parts.append(f"<source{attrs}>{inner}</source>" if inner else f"<source{attrs}/>")
    return "".join(parts)


def _timespan_xml(ts: Timespan | None) -> str:
    if not ts:
        return ""

    def time_xml(tag: str, t: Time | None) -> str:
        if not t:
            return ""
        return f'<{tag} jd="{t.jd}" precision="{t.precision}"/>'

    return f"<timespan>{time_xml('begin', ts.begin)}{time_xml('end', ts.end)}</timespan>"


def _year_attrs(begin_year: int | None, end_year: int | None, year: int | None) -> str:
    attrs = ""
    if begin_year is not None:
        attrs += f' begin-year="{begin_year}"'
    if end_year is not None:
        attrs += f' end-year="{end_year}"'
    if year is not None:
        attrs += f' year="{year}"'
    return attrs


def _property_xml(tag: str, prop: Property) -> str:
    attrs = ""
    if prop.lang is not None:
        attrs += f' lang="{escape(prop.lang)}"'
    if prop.value is not None:
        attrs += f' value="{escape(str(prop.value))}"'
    attrs += _year_attrs(prop.begin_year, prop.end_year, prop.year)
    inner = _timespan_xml(prop.timespan) + _source_refs_xml(prop.source)
    return f"<{tag}{attrs}>{inner}</{tag}>" if inner else f"<{tag}{attrs}/>"


def _relation_xml(tag: str, rel: Relation) -> str:
    attrs = f' ref="{escape(rel.ref)}"' + _year_attrs(rel.begin_year, rel.end_year, rel.year)
    inner = _timespan_xml(rel.timespan) + _source_refs_xml(rel.source)
    return f"<{tag}{attrs}>{inner}</{tag}>" if inner else f"<{tag}{attrs}/>"


def _note_xml(tag: str, note: Note) -> str:
    attrs = f' lang="{escape(note.lang)}"' if note.lang is not None else ""
    inner = f"<text>{escape(note.text)}</text>" + _source_refs_xml(note.source)
    return f"<{tag}{attrs}>{inner}</{tag}>"


def gov_object_to_xml(obj: GovObject) -> str:
    """Serialize a GovObject back into the <object> XML ChangeService.saveObject
    expects. Round-trips every field so unrelated data isn't dropped on save
    (saveObject replaces the whole object, it doesn't patch)."""
    attrs = f' id="{escape(obj.id)}"'
    if obj.last_modification:
        attrs += f' last-modification="{escape(obj.last_modification)}"'
    if obj.deprecated:
        attrs += f' deprecated="{escape(obj.deprecated)}"'

    parts = []
    if obj.position:
        p = obj.position
        pattrs = f' lon="{p.lon}" lat="{p.lat}"'
        if p.height is not None:
            pattrs += f' height="{p.height}"'
        if p.type is not None:
            pattrs += f' type="{escape(p.type)}"'
        parts.append(f"<position{pattrs}/>")

    for json_key, xml_tag in _PROPERTY_FIELDS:
        for item in getattr(obj, json_key):
            parts.append(_property_xml(xml_tag, item))
    for json_key, xml_tag in _RELATION_FIELDS:
        for item in getattr(obj, json_key):
            parts.append(_relation_xml(xml_tag, item))
    for json_key, xml_tag in _NOTE_FIELDS:
        for item in getattr(obj, json_key):
            parts.append(_note_xml(xml_tag, item))

    return f'<object xmlns="http://gov.genealogy.net/data"{attrs}>{"".join(parts)}</object>'


# ---------------------------------------------------------------------------
# GOV read/write
# ---------------------------------------------------------------------------

def get_gov_object(item_id: str) -> GovObject:
    r = requests.get(f"{GOV_API}/getObject", params={"itemId": item_id},
                      headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_gov_object(r.json())


def gov_wikidata_qid(obj: GovObject) -> str | None:
    """Return the QID already linked in obj.external_reference, if any."""
    for ref in obj.external_reference:
        if ref.value and ref.value.startswith("wikidata:"):
            return ref.value.split(":", 1)[1]
    return None


def save_gov_object(obj_xml: str) -> str:
    """POST saveObject. Returns the saved object's id. Raises on SOAP fault."""
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ws="http://gov.genealogy.net/ws">'
        "<soapenv:Body><ws:saveObject>"
        f"{obj_xml}"
        f"<username>{escape(GOV_USERNAME)}</username>"
        f"<password>{escape(GOV_API_KEY)}</password>"
        "</ws:saveObject></soapenv:Body></soapenv:Envelope>"
    )
    r = requests.post(
        GOV_CHANGE_SERVICE,
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
        timeout=30,
    )
    r.raise_for_status()
    if "soap:Fault" in r.text or "Fault" in r.text and "<faultstring>" in r.text:
        raise RuntimeError(f"SOAP fault: {r.text}")
    return r.text


def link_gov_to_wikidata(item_id: str, qid: str, dry_run: bool = True) -> str:
    """Add external-reference value="wikidata:{qid}" to a GOV object.
    No-op if that exact reference is already present."""
    obj = get_gov_object(item_id)
    existing_qid = gov_wikidata_qid(obj)
    if existing_qid == qid:
        return "already-linked"
    if existing_qid and existing_qid != qid:
        return f"conflict: already linked to wikidata:{existing_qid}"

    obj.external_reference.append(Property(value=f"wikidata:{qid}"))
    obj_xml = gov_object_to_xml(obj)

    if dry_run:
        return "dry-run"

    save_gov_object(obj_xml)
    return "linked"


# ---------------------------------------------------------------------------
# Wikidata read/write
# ---------------------------------------------------------------------------

def _wikidata_site():
    """Log into Wikidata with the BotPassword credentials from .env, using
    pywikibot's actual supported mechanism: a generated password file (see
    https://www.mediawiki.org/wiki/Manual:Pywikibot/BotPasswords). Passing the
    password to one manually-built LoginManager isn't enough — pywikibot can
    trigger its own fresh, password-less login internally (e.g. while fetching
    tokens or retrying a notloggedin API response), and that one falls back to
    an interactive prompt unless config.password_file is set, since that's
    where *every* LoginManager instance looks first."""
    import os
    import tempfile

    import pywikibot

    if not (WD_USERNAME and WD_BOT_PASSWORD):
        raise RuntimeError("WD_USERNAME / WD_BOT_PASSWORD not set in .env")
    if "@" not in WD_USERNAME:
        raise RuntimeError("WD_USERNAME must be 'mainuser@botname' (a BotPassword login)")
    bare_username, _, suffix = WD_USERNAME.partition("@")

    pywikibot.config.usernames["wikidata"]["wikidata"] = bare_username
    fd, pw_path = tempfile.mkstemp(suffix=".pwfile")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"('{bare_username}', BotPassword('{suffix}', '{WD_BOT_PASSWORD}'))\n")
        os.chmod(pw_path, 0o600)
        pywikibot.config.password_file = pw_path
        site = pywikibot.Site("wikidata", "wikidata")
        site.login()
    finally:
        os.remove(pw_path)
    return site


def _wbgetentities_claims(ids_batch: list[str]) -> dict:
    """GET wbgetentities for <=50 ids, props=claims only (no labels/sitelinks/etc
    — full ItemPage.get() per id is what triggered 429s under load)."""
    for attempt in range(5):
        r = requests.get(
            WD_API,
            params={
                "action": "wbgetentities",
                "ids": "|".join(ids_batch),
                "props": "claims",
                "format": "json",
                "maxlag": 5,
            },
            headers=HEADERS,
            timeout=30,
        )
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 4:
                r.raise_for_status()
            wait = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def wikidata_gov_ids_bulk(qids: list[str]) -> dict[str, str | None]:
    """Bulk-check which qids already have a P2503 (GOV identifier) claim.
    Batches 50 ids per request — use this instead of N calls to wikidata_gov_id()
    when checking many candidates, to stay under Wikidata's rate limits."""
    result: dict[str, str | None] = {}
    unique_qids = list(dict.fromkeys(qids))
    n_batches = (len(unique_qids) + 49) // 50
    for i in range(0, len(unique_qids), 50):
        batch = unique_qids[i:i + 50]
        print(f"  Wikidata existing-link check: batch {i // 50 + 1}/{n_batches} ({len(batch)} qids)")
        data = _wbgetentities_claims(batch)
        for qid, entity in data.get("entities", {}).items():
            claims = entity.get("claims", {}).get(GOV_P2503, [])
            result[qid] = claims[0]["mainsnak"]["datavalue"]["value"] if claims else None
        if i + 50 < len(unique_qids):
            time.sleep(0.5)  # be polite between batches
    return result


def wikidata_gov_id(qid: str) -> str | None:
    """Return the GOV id already linked via P2503, if any. For checking many
    qids at once, use wikidata_gov_ids_bulk() instead — far fewer requests."""
    return wikidata_gov_ids_bulk([qid]).get(qid)


def link_wikidata_to_gov(qid: str, gov_id: str, dry_run: bool = True, existing: str | None = _UNSET) -> str:
    """Add a P2503 (GOV identifier) claim to a Wikidata item.
    No-op if that exact claim is already present. Pass `existing` (the result
    of a prior wikidata_gov_ids_bulk() lookup) to skip the read here."""
    import pywikibot

    if existing is _UNSET:
        existing = wikidata_gov_id(qid)
    if existing == gov_id:
        return "already-linked"
    if existing and existing != gov_id:
        return f"conflict: already linked to GOV id {existing}"

    if dry_run:
        return "dry-run"

    site = _wikidata_site()
    item = pywikibot.ItemPage(site, qid)
    item.get()
    claim = pywikibot.Claim(site, GOV_P2503)
    claim.setTarget(gov_id)
    item.addClaim(claim, summary="Add GOV identifier (gov-wikidata-match)")
    return "linked"


# ---------------------------------------------------------------------------
# Scored-candidates JSON -> writes. See README "Output format" for the
# gov_wikidata_match.py JSON shape these dataclasses parse.
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    qid: str
    label: str
    type_qids: list[str]
    type_labels: list[str]
    lat: float
    lon: float
    distance_m: float
    score: float


@dataclass
class ScoredEntry:
    gov_id: str
    gov_names: list[str]
    gov_types: list[int]
    gov_lat: float
    gov_lon: float
    candidates: list[Candidate]


def load_scored_entries(scored_path: str) -> list[ScoredEntry]:
    with open(scored_path) as f:
        raw = json.load(f)
    return [
        ScoredEntry(
            gov_id=e["gov_id"],
            gov_names=e["gov_names"],
            gov_types=e["gov_types"],
            gov_lat=e["gov_lat"],
            gov_lon=e["gov_lon"],
            candidates=[Candidate(**c) for c in e["candidates"]],
        )
        for e in raw
    ]


def iter_selected(entries: list[ScoredEntry], passes_filters: Callable[[ScoredEntry, Candidate], bool]):
    for entry in entries:
        for candidate in entry.candidates:
            if passes_filters(entry, candidate):
                yield entry, candidate


@dataclass
class WriteResult:
    entry: ScoredEntry
    candidate: Candidate
    gov_status: str
    wd_status: str


def write_match(entry: ScoredEntry, candidate: Candidate, dry_run: bool,
                 existing_wd_gov_id: str | None = _UNSET) -> WriteResult:
    gov_status = link_gov_to_wikidata(entry.gov_id, candidate.qid, dry_run=dry_run)
    wd_status = link_wikidata_to_gov(candidate.qid, entry.gov_id, dry_run=dry_run,
                                      existing=existing_wd_gov_id)
    print(f"{entry.gov_id} <-> {candidate.qid}  GOV:{gov_status}  WD:{wd_status}")
    return WriteResult(entry=entry, candidate=candidate, gov_status=gov_status, wd_status=wd_status)


def run_writes(
    entries: list[ScoredEntry],
    passes_filters: Callable[[ScoredEntry, Candidate], bool],
    object_id: str | None = None,
    dry_run: bool = True,
    skip: int = 0,
    limit: int | None = None,
) -> list[WriteResult]:
    """Write every (entry, candidate) pair that passes_filters() selects.
    `skip`/`limit` slice the selected (post-filter) list, for processing a
    large file in batches across multiple runs — already-linked matches are
    no-ops regardless, so batch boundaries don't need to track what's been
    done already. A failure on one match is logged as wd_status="error: ..."
    and does not abort the rest of the batch. Returns one WriteResult per
    match attempted."""
    selected = [
        (entry, candidate)
        for entry, candidate in iter_selected(entries, passes_filters)
        if not object_id or entry.gov_id == object_id
    ]
    total_selected = len(selected)
    selected = selected[skip:skip + limit if limit is not None else None]
    print(f"{total_selected} match(es) selected by filters; processing {len(selected)} "
          f"(skip={skip}, limit={limit}). Checking Wikidata for existing links…")
    # Batch-check Wikidata's side up front (50 qids/request) instead of one
    # heavyweight ItemPage.get() per candidate — avoids tripping rate limits.
    existing_links = wikidata_gov_ids_bulk([c.qid for _, c in selected])

    results = []
    for entry, candidate in selected:
        try:
            result = write_match(entry, candidate, dry_run=dry_run,
                                  existing_wd_gov_id=existing_links.get(candidate.qid))
        except Exception as e:
            print(f"{entry.gov_id} <-> {candidate.qid}  ERROR: {e}")
            result = WriteResult(entry=entry, candidate=candidate,
                                  gov_status="error", wd_status=f"error: {e}")
        results.append(result)
    return results
