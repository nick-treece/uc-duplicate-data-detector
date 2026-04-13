"""Duplicate detection engine using column-name Jaccard similarity, schema structure
matching, and fuzzy table-name comparison. Groups tables into duplicate clusters
and scores them for gold-standard recommendation."""

from __future__ import annotations
import re
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from itertools import combinations

from server.scanner import TableInfo


def _normalize_col(name: str) -> str:
    """Normalize a column name for comparison: lowercase, strip underscores/prefixes."""
    name = name.lower().strip("_")
    for prefix in ("raw_", "dim_", "fact_", "_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


# Maps common synonym pairs so renamed columns still match
_SYNONYMS = {
    "student_id": {"learner_id", "pupil_id"},
    "first_name": {"given_name", "pupil_first_name", "firstname"},
    "last_name": {"family_name", "pupil_last_name", "lastname"},
    "date_of_birth": {"dob", "pupil_dob"},
    "is_sen": {"has_send"},
    "fsm_eligible": {"pupil_premium"},
    "school_id": {"establishment_id"},
    "year_group": {"national_curriculum_year"},
    "score": {"mark", "rawscore", "percentage_score"},
    "grade": {"result_grade", "final_grade"},
    "exam_board": {"awarding_body"},
    "attendance_id": {"record_id", "recordid"},
    "status": {"attendancecode", "attendance_mark"},
    "session": {"am_pm"},
    "term": {"half_term"},
    "student_id": {"studentid", "learner_id", "pupil_id"},
    "school_name": {"schoolname"},
    "result_id": {"resultid"},
}

_REVERSE_SYNONYMS: dict[str, str] = {}
for canonical, synonyms in _SYNONYMS.items():
    for syn in synonyms:
        _REVERSE_SYNONYMS[syn] = canonical
    _REVERSE_SYNONYMS[canonical] = canonical


def _canonical_col(name: str) -> str:
    n = _normalize_col(name)
    return _REVERSE_SYNONYMS.get(n, n)


def column_similarity(cols_a: list[str], cols_b: list[str]) -> float:
    """Jaccard similarity on canonical column names, excluding metadata columns."""
    skip = {"_source_file", "_ingestion_ts", "_source", "_data_source", "_source_system"}
    set_a = {_canonical_col(c) for c in cols_a if c.lower() not in skip}
    set_b = {_canonical_col(c) for c in cols_b if c.lower() not in skip}
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _type_str(t) -> str:
    """Convert a type_name to a lowercase string, handling enums."""
    if hasattr(t, 'value'):
        return str(t.value).lower()
    return str(t).lower()


def type_similarity(table_a: TableInfo, table_b: TableInfo) -> float:
    """Compare column types for columns that match by canonical name."""
    map_a = {_canonical_col(c.name): _type_str(c.type_name) for c in table_a.columns}
    map_b = {_canonical_col(c.name): _type_str(c.type_name) for c in table_b.columns}
    shared = set(map_a.keys()) & set(map_b.keys())
    if not shared:
        return 0.0
    matches = sum(1 for k in shared if _types_compatible(map_a[k], map_b[k]))
    return matches / len(shared)


def _types_compatible(t1: str, t2: str) -> bool:
    if t1 == t2:
        return True
    numeric = {"int", "long", "bigint", "double", "float", "decimal", "short", "integer"}
    if t1 in numeric and t2 in numeric:
        return True
    string_like = {"string", "varchar", "char", "text"}
    if t1 in string_like and t2 in string_like:
        return True
    return False


def _tokenize_name(name: str) -> set[str]:
    parts = re.split(r"[_\s-]+", name.lower())
    return {p for p in parts if p not in ("raw", "dim", "fact", "agg")}


def name_similarity(name_a: str, name_b: str) -> float:
    """Token-based name similarity, stripping common prefixes."""
    tokens_a = _tokenize_name(name_a)
    tokens_b = _tokenize_name(name_b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


@dataclass
class DuplicatePair:
    table_a: str
    table_b: str
    column_similarity: float
    type_similarity: float
    name_similarity: float
    composite_score: float


@dataclass
class DuplicateGroup:
    group_id: int
    label: str
    tables: list[str]
    pairs: list[DuplicatePair]
    gold_standard: str | None = None
    gold_scores: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "group_id": self.group_id,
            "label": self.label,
            "tables": self.tables,
            "pairs": [asdict(p) for p in self.pairs],
            "gold_standard": self.gold_standard,
            "gold_scores": self.gold_scores,
        }


def _build_candidate_pairs(
    tables: list[TableInfo],
    max_group_size: int = 500,
) -> set[tuple[int, int]]:
    """Pre-filter: group tables by normalised name, compare within groups.

    Tables whose sorted token set is identical are placed in the same
    group.  Only cross-catalog/schema pairs within each group become
    candidates.  Groups larger than *max_group_size* are skipped.

    This is dramatically faster than the single-token inverted-index
    approach at scale (\u223c100K candidates vs \u223c15M).
    """
    from collections import defaultdict

    name_groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i, t in enumerate(tables):
        key = tuple(sorted(_tokenize_name(t.name)))
        if key:  # skip tables with empty token sets
            name_groups[key].append(i)

    candidates: set[tuple[int, int]] = set()
    for key, indices in name_groups.items():
        if len(indices) < 2 or len(indices) > max_group_size:
            continue
        for a, b in combinations(indices, 2):
            ta, tb = tables[a], tables[b]
            if ta.catalog == tb.catalog and ta.schema == tb.schema:
                continue
            candidates.add((min(a, b), max(a, b)))
            if len(candidates) % 10000 == 0:
                time.sleep(0)  # yield GIL for HTTP threads

    return candidates


def detect_duplicates(
    tables: list[TableInfo],
    threshold: float = 0.5,
    col_weight: float = 0.50,
    type_weight: float = 0.30,
    name_weight: float = 0.20,
) -> list[DuplicateGroup]:
    """Find duplicate table groups based on composite similarity.

    Uses a name-token pre-filter to avoid O(n²) comparisons at scale.
    Only tables sharing at least one name token are compared.
    """
    candidates = _build_candidate_pairs(tables)
    pairs: list[DuplicatePair] = []

    for i, (a, b) in enumerate(candidates):
        if i % 5000 == 0:
            time.sleep(0)  # yield GIL for HTTP threads

        ta, tb = tables[a], tables[b]

        cols_a = [c.name for c in ta.columns]
        cols_b = [c.name for c in tb.columns]
        col_sim = column_similarity(cols_a, cols_b)
        typ_sim = type_similarity(ta, tb)
        nm_sim = name_similarity(ta.name, tb.name)

        composite = col_sim * col_weight + typ_sim * type_weight + nm_sim * name_weight
        if composite >= threshold:
            pairs.append(DuplicatePair(
                table_a=ta.full_name,
                table_b=tb.full_name,
                column_similarity=round(col_sim, 3),
                type_similarity=round(typ_sim, 3),
                name_similarity=round(nm_sim, 3),
                composite_score=round(composite, 3),
            ))

    groups = _cluster_pairs(pairs)

    table_map = {t.full_name: t for t in tables}
    for group in groups:
        group_tables = [table_map[n] for n in group.tables if n in table_map]
        scores = score_gold_standard(group_tables)
        group.gold_scores = scores
        if scores:
            group.gold_standard = max(scores, key=scores.get)

    return groups


def _derive_group_label(full_names: list[str]) -> str:
    """Derive a human-readable label from the table names in a duplicate group.

    Tokenises each table name (stripping common prefixes like raw_, dim_, fact_),
    then picks the tokens that appear across the most tables.
    """
    short_names = [n.split(".")[-1] for n in full_names]
    token_sets = [_tokenize_name(n) for n in short_names]

    counts: Counter[str] = Counter()
    for tokens in token_sets:
        for t in tokens:
            counts[t] += 1

    min_freq = max(2, len(full_names) * 0.4)
    common = [tok for tok, cnt in counts.most_common() if cnt >= min_freq]

    if common:
        return " ".join(common[:3]).replace("_", " ").title()

    shortest = min(short_names, key=len)
    return re.sub(r"[_\-]+", " ", shortest).title()


def _cluster_pairs(pairs: list[DuplicatePair]) -> list[DuplicateGroup]:
    """Union-find clustering of table pairs into groups."""
    parent: dict[str, str] = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for p in pairs:
        parent.setdefault(p.table_a, p.table_a)
        parent.setdefault(p.table_b, p.table_b)
        union(p.table_a, p.table_b)

    clusters: dict[str, list[str]] = {}
    for node in parent:
        root = find(node)
        clusters.setdefault(root, []).append(node)

    groups = []
    for i, (_, members) in enumerate(sorted(clusters.items())):
        group_pairs = [p for p in pairs if p.table_a in members or p.table_b in members]
        sorted_members = sorted(members)
        groups.append(DuplicateGroup(
            group_id=i + 1,
            label=_derive_group_label(sorted_members),
            tables=sorted_members,
            pairs=group_pairs,
        ))

    return groups


def _parse_ts(value) -> float:
    """Convert an updated_at value to a Unix timestamp for arithmetic."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    # String timestamps from the SQL Statement API
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def score_gold_standard(tables: list[TableInfo]) -> dict[str, float]:
    """Score each table in a duplicate group for gold-standard recommendation.

    Scoring factors (higher is better):
      - Column count: more columns \u2192 more complete
      - Updated recently: more recent \u2192 more maintained
    """
    scores: dict[str, float] = {}

    for t in tables:
        s = 0.0

        # Column count: more columns often means more complete
        all_col_counts = [len(tt.columns) for tt in tables]
        max_cols = max(all_col_counts) if all_col_counts else 1
        if max_cols > 0:
            s += (len(t.columns) / max_cols) * 10

        # Recently updated: prefer tables that are actively maintained
        ts = _parse_ts(t.updated_at)
        if ts > 0:
            all_ts = [_parse_ts(tt.updated_at) for tt in tables]
            all_ts = [v for v in all_ts if v > 0]
            if all_ts:
                max_ts = max(all_ts)
                min_ts = min(all_ts)
                span = max_ts - min_ts
                if span > 0:
                    s += ((ts - min_ts) / span) * 10
                else:
                    s += 10

        scores[t.full_name] = round(s, 1)

    return scores
