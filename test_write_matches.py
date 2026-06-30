"""Unit test for write_matches.py's only real logic: passes_filters().
Loading/iteration/writing live in gov_write.py and are tested there/manually.
"""

from gov_write import Candidate, ScoredEntry
from write_matches import passes_filters


def _entry(score: float, distance_m: float, n_candidates: int = 1) -> tuple[ScoredEntry, Candidate]:
    candidate = Candidate(
        qid="Q1", label="x", type_qids=[], type_labels=[],
        lat=0.0, lon=0.0, distance_m=distance_m, score=score,
    )
    candidates = [candidate] + [
        Candidate(qid=f"Q{i}", label="x", type_qids=[], type_labels=[],
                   lat=0.0, lon=0.0, distance_m=distance_m, score=score)
        for i in range(2, n_candidates + 1)
    ]
    entry = ScoredEntry(gov_id="G1", gov_names=["x"], gov_types=[26],
                         gov_lat=0.0, gov_lon=0.0, candidates=candidates)
    return entry, candidate


# Bound-spanning values (best/worst possible score and distance) so these
# stay valid regardless of where the thresholds in passes_filters() are tuned.
def test_passes_filters_accepts_perfect_close_unambiguous_match():
    entry, candidate = _entry(score=1.0, distance_m=0.0)
    assert passes_filters(entry, candidate) is True


def test_passes_filters_rejects_low_score():
    entry, candidate = _entry(score=0.0, distance_m=0.0)
    assert passes_filters(entry, candidate) is False


def test_passes_filters_rejects_far_distance():
    entry, candidate = _entry(score=1.0, distance_m=10_000)
    assert passes_filters(entry, candidate) is False


def test_passes_filters_rejects_ambiguous_entry_with_multiple_candidates():
    entry, candidate = _entry(score=1.0, distance_m=0.0, n_candidates=2)
    assert passes_filters(entry, candidate) is False
