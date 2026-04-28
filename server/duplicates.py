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



# ── Lineage-based similarity ──────────────────────────────────────────


def lineage_similarity(
    table_a: str,
    table_b: str,
    upstream_map: dict[str, set[str]],
    downstream_map: dict[str, set[str]],
    transitive_upstream: dict[str, dict[str, int]] | None = None,
) -> float:
    """Score lineage relationship between two tables.

    Returns a value between 0.0 and 1.0 based on:
      - Direct lineage (one feeds the other → 1.0)
      - Shared transitive ancestors weighted by depth (if available)
      - Fallback: single-hop shared upstream Jaccard
    """
    a_lower = table_a.lower()
    b_lower = table_b.lower()

    # Direct lineage: A→B or B→A
    a_downstream = downstream_map.get(a_lower, set())
    b_downstream = downstream_map.get(b_lower, set())
    if b_lower in a_downstream or a_lower in b_downstream:
        return 1.0

    # Transitive ancestor scoring (depth-weighted)
    if transitive_upstream:
        a_ancestors = transitive_upstream.get(a_lower, {})
        b_ancestors = transitive_upstream.get(b_lower, {})

        if a_ancestors or b_ancestors:
            shared_keys = set(a_ancestors.keys()) & set(b_ancestors.keys())
            if shared_keys:
                # Weight closer ancestors higher: 1/depth
                weighted_score = sum(
                    1.0 / max(a_ancestors[k], b_ancestors[k])
                    for k in shared_keys
                )
                # Normalise against the smaller ancestor set
                all_keys = set(a_ancestors.keys()) | set(b_ancestors.keys())
                max_possible = sum(
                    1.0 / min(
                        a_ancestors.get(k, 999),
                        b_ancestors.get(k, 999),
                    )
                    for k in all_keys
                )
                if max_possible > 0:
                    return min(weighted_score / max_possible, 1.0)
            # Both have ancestors but no overlap
            if a_ancestors and b_ancestors:
                return 0.0

    # Fallback: single-hop Jaccard
    a_upstream = upstream_map.get(a_lower, set())
    b_upstream = upstream_map.get(b_lower, set())

    if not a_upstream and not b_upstream:
        return 0.0

    intersection = a_upstream & b_upstream
    union = a_upstream | b_upstream

    if not union:
        return 0.0

    return len(intersection) / len(union)



@dataclass
class DuplicatePair:
    table_a: str
    table_b: str
    column_similarity: float
    type_similarity: float
    name_similarity: float
    lineage_similarity: float
    composite_score: float


@dataclass
class DuplicateGroup:
    group_id: int
    label: str
    tables: list[str]
    pairs: list[DuplicatePair]
    gold_standard: str | None = None
    gold_scores: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "group_id": self.group_id,
            "label": self.label,
            "tables": self.tables,
            "pairs": [asdict(p) for p in self.pairs],
            "gold_standard": self.gold_standard,
            "gold_scores": self.gold_scores,
            "tags": self.tags,
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



def _build_lineage_candidate_pairs(
    tables: list[TableInfo],
    transitive_upstream: dict[str, dict[str, int]],
    max_ancestor_fanout: int = 100,
    min_shared_ratio: float = 0.5,
    top_n_ancestors: int = 5,
    max_candidates: int = 200_000,
) -> set[tuple[int, int]]:
    """Second candidate-generation pass: pair tables sharing deep ancestors.

    Groups tables by their closest transitive ancestors (depth <= 3 only).
    If two tables share >50% of their top-5 ancestors, they become candidates
    regardless of name similarity.

    Guard rails:
      - Only ancestors at depth <= 3
      - Skip ancestors appearing in >max_ancestor_fanout tables (hub sources)
      - Cap total candidates to avoid explosion
    """
    from collections import defaultdict

    # Step 1: Count how many tables each ancestor appears in (for hub filtering)
    ancestor_frequency: dict[str, int] = defaultdict(int)
    for table_ancestors in transitive_upstream.values():
        for anc, depth in table_ancestors.items():
            if depth <= 3:
                ancestor_frequency[anc] += 1

    # Step 2: For each table, compute its "ancestor signature" (top-N closest, non-hub)
    table_index = {t.full_name.lower(): i for i, t in enumerate(tables)}
    signatures: dict[int, tuple[str, ...]] = {}

    for i, t in enumerate(tables):
        key = t.full_name.lower()
        ancestors = transitive_upstream.get(key, {})
        if not ancestors:
            continue

        # Filter: depth <= 3, skip hubs
        filtered = [
            (anc, depth) for anc, depth in ancestors.items()
            if depth <= 3 and ancestor_frequency[anc] <= max_ancestor_fanout
        ]
        if not filtered:
            continue

        # Take top-N closest ancestors (sorted by depth, then name for stability)
        filtered.sort(key=lambda x: (x[1], x[0]))
        top = tuple(anc for anc, _ in filtered[:top_n_ancestors])
        signatures[i] = top

    # Step 3: Inverted index — map each ancestor to the tables that have it in their signature
    ancestor_to_tables: dict[str, list[int]] = defaultdict(list)
    for idx, sig in signatures.items():
        for anc in sig:
            ancestor_to_tables[anc].append(idx)

    # Step 4: Find pairs sharing enough ancestors
    candidates: set[tuple[int, int]] = set()
    seen_pairs: set[tuple[int, int]] = set()

    for idx_a, sig_a in signatures.items():
        if len(candidates) >= max_candidates:
            break

        # Collect all tables that share at least one ancestor with idx_a
        neighbour_counts: dict[int, int] = defaultdict(int)
        for anc in sig_a:
            for idx_b in ancestor_to_tables[anc]:
                if idx_b != idx_a:
                    neighbour_counts[idx_b] += 1

        sig_a_len = len(sig_a)
        for idx_b, shared_count in neighbour_counts.items():
            pair_key = (min(idx_a, idx_b), max(idx_a, idx_b))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # Check ratio against the smaller signature
            sig_b = signatures.get(idx_b)
            if not sig_b:
                continue
            min_sig_len = min(sig_a_len, len(sig_b))
            if min_sig_len == 0:
                continue

            ratio = shared_count / min_sig_len
            if ratio < min_shared_ratio:
                continue

            # Only cross-catalog/schema pairs
            ta, tb = tables[idx_a], tables[idx_b]
            if ta.catalog == tb.catalog and ta.schema == tb.schema:
                continue

            candidates.add(pair_key)

            if len(candidates) % 10000 == 0:
                time.sleep(0)

    return candidates

# ── Group tagging ─────────────────────────────────────────────────────────

_MEDALLION_KEYWORDS = {"gold", "silver", "bronze"}


def _get_medallion_tier(catalog_name: str) -> str | None:
    """Return the medallion tier if the catalog name contains one, else None."""
    lower = catalog_name.lower()
    for tier in _MEDALLION_KEYWORDS:
        if tier in lower:
            return tier
    return None


def _tag_governance_views(
    groups: list[DuplicateGroup],
    table_map: dict[str, TableInfo],
    column_lineage_map: dict | None = None,
):
    """Tag 2-table groups where a VIEW's columns are a subset of the paired TABLE.

    When column_lineage_map is available, also checks whether the view's
    columns actually trace back to the paired table (stronger confirmation).
    """
    col_lineage = column_lineage_map or {}

    for group in groups:
        if len(group.tables) != 2:
            continue

        ta = table_map.get(group.tables[0])
        tb = table_map.get(group.tables[1])
        if not ta or not tb:
            continue

        if ta.table_type.upper() == "VIEW" and tb.table_type.upper() != "VIEW":
            view, tbl = ta, tb
        elif tb.table_type.upper() == "VIEW" and ta.table_type.upper() != "VIEW":
            view, tbl = tb, ta
        else:
            continue

        view_cols = {_canonical_col(c.name) for c in view.columns}
        tbl_cols = {_canonical_col(c.name) for c in tbl.columns}
        if not (view_cols and view_cols <= tbl_cols):
            continue

        # Column lineage confirmation (if available)
        lineage_key = (tbl.full_name.lower(), view.full_name.lower())
        col_mappings = col_lineage.get(lineage_key, [])

        if col_mappings:
            # Lineage confirms: table feeds the view
            group.tags.append("governance_view")
            group.tags.append("lineage_confirmed")
        else:
            # No lineage data — fall back to schema-only heuristic
            group.tags.append("governance_view")


def _tag_pipeline_stages(
    groups: list[DuplicateGroup],
    downstream_map: dict[str, set[str]] | None = None,
):
    """Tag groups where tables form a pipeline chain.

    Two detection methods (lineage-first, heuristic fallback):

    1. **Lineage**: if any table in the group directly feeds another
       member, the relationship is a confirmed pipeline stage.
    2. **Medallion heuristic** (fallback): if every table is in a
       gold/silver/bronze catalog spanning 2+ tiers.
    """
    downstream = downstream_map or {}

    for group in groups:
        # ── Lineage-based detection ───────────────────────────────────
        if downstream:
            has_direct_edge = False
            for full_name in group.tables:
                targets = downstream.get(full_name.lower(), set())
                other_members = {t.lower() for t in group.tables if t != full_name}
                if targets & other_members:
                    has_direct_edge = True
                    break
            if has_direct_edge:
                group.tags.append("pipeline_stage")
                continue  # skip heuristic — lineage is definitive

        # ── Medallion heuristic (fallback) ────────────────────────────
        tiers: set[str] = set()
        all_medallion = True

        for full_name in group.tables:
            catalog = full_name.split(".")[0]
            tier = _get_medallion_tier(catalog)
            if tier:
                tiers.add(tier)
            else:
                all_medallion = False
                break

        if all_medallion and len(tiers) >= 2:
            group.tags.append("pipeline_stage")


def _tag_shared_source(
    groups: list[DuplicateGroup],
    upstream_map: dict[str, set[str]] | None = None,
):
    """Tag groups where all members trace back to the same upstream table.

    Distinguishes 'independent copies of the same source' from tables
    that just happen to look similar.  Only applied when lineage data
    is available and every table in the group has at least one upstream.
    """
    if not upstream_map:
        return

    for group in groups:
        upstreams_per_table = []
        for full_name in group.tables:
            ups = upstream_map.get(full_name.lower(), set())
            if not ups:
                break  # can't confirm shared source if any table has no lineage
            upstreams_per_table.append(ups)
        else:
            # All tables have upstream data — check for a common ancestor
            shared = upstreams_per_table[0]
            for ups in upstreams_per_table[1:]:
                shared = shared & ups
            if shared:
                group.tags.append("shared_source")


def _apply_tags(
    groups: list[DuplicateGroup],
    table_map: dict[str, TableInfo],
    lineage: dict | None = None,
):
    """Run all taggers on the given groups."""
    upstream_map = (lineage or {}).get("upstream_map")
    downstream_map = (lineage or {}).get("downstream_map")
    column_lineage_map = (lineage or {}).get("column_lineage_map")

    _tag_governance_views(groups, table_map, column_lineage_map=column_lineage_map)
    _tag_pipeline_stages(groups, downstream_map=downstream_map)
    _tag_shared_source(groups, upstream_map=upstream_map)


def detect_duplicates(
    tables: list[TableInfo],
    threshold: float = 0.5,
    col_weight: float = 0.40,
    type_weight: float = 0.25,
    name_weight: float = 0.15,
    lin_weight: float = 0.20,
    lineage: dict | None = None,
) -> list[DuplicateGroup]:
    """Find duplicate table groups based on composite similarity.

    Uses a name-token pre-filter to avoid O(n²) comparisons at scale.
    Only tables sharing at least one name token are compared.

    The *lineage* dict should contain ``upstream_map``,
    ``downstream_map``, and ``consumer_counts`` (all keyed by
    lowercase full table name).  Lineage weight is applied
    per-pair: if neither table in a pair has lineage data,
    the lineage weight is redistributed to the other three
    components so those pairs are scored identically to the
    pre-lineage algorithm.
    """
    upstream_map = (lineage or {}).get("upstream_map", {})
    downstream_map = (lineage or {}).get("downstream_map", {})
    transitive_upstream = (lineage or {}).get("transitive_upstream", {})

    # Pre-compute which tables have any lineage data at all
    tables_with_lineage = set(upstream_map.keys()) | set(downstream_map.keys())

    # Pre-compute renormalised weights for pairs with no lineage
    base_total = col_weight + type_weight + name_weight
    no_lin_cw = col_weight / base_total if base_total else 0.5
    no_lin_tw = type_weight / base_total if base_total else 0.3
    no_lin_nw = name_weight / base_total if base_total else 0.2

    candidates = _build_candidate_pairs(tables)

    # Enhancement 2: add lineage-based candidates (different names, shared ancestors)
    if transitive_upstream:
        lineage_candidates = _build_lineage_candidate_pairs(
            tables, transitive_upstream
        )
        candidates = candidates | lineage_candidates

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

        # Per-pair lineage: only score if at least one table has lineage
        a_has = ta.full_name.lower() in tables_with_lineage
        b_has = tb.full_name.lower() in tables_with_lineage
        pair_has_lineage = a_has or b_has

        lin_sim = 0.0
        if pair_has_lineage:
            lin_sim = lineage_similarity(
                ta.full_name, tb.full_name, upstream_map, downstream_map,
                transitive_upstream=transitive_upstream,
            )

        # Use full 4-component weights when lineage applies,
        # otherwise renormalise to 3 components (no penalty)
        if pair_has_lineage:
            cw, tw, nw, lw = col_weight, type_weight, name_weight, lin_weight
        else:
            cw, tw, nw, lw = no_lin_cw, no_lin_tw, no_lin_nw, 0.0

        composite = (
            col_sim * cw + typ_sim * tw + nm_sim * nw + lin_sim * lw
        )
        if composite >= threshold:
            pairs.append(DuplicatePair(
                table_a=ta.full_name,
                table_b=tb.full_name,
                column_similarity=round(col_sim, 3),
                type_similarity=round(typ_sim, 3),
                name_similarity=round(nm_sim, 3),
                lineage_similarity=round(lin_sim, 3),
                composite_score=round(composite, 3),
            ))

    groups = _cluster_pairs(pairs)

    table_map = {t.full_name: t for t in tables}
    for group in groups:
        group_tables = [table_map[n] for n in group.tables if n in table_map]
        scores = score_gold_standard(group_tables, lineage=lineage)
        group.gold_scores = scores
        if scores:
            group.gold_standard = max(scores, key=scores.get)

    # ── Tag groups for filtering ──────────────────────────────────────────
    _apply_tags(groups, table_map, lineage=lineage)

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


def score_gold_standard(
    tables: list[TableInfo],
    lineage: dict | None = None,
) -> dict[str, float]:
    """Score each table in a duplicate group for gold-standard recommendation.

    Scoring factors (higher is better, each out of 10):
      - Column completeness: more columns → more complete
      - Freshness: recently updated → more maintained
      - Consumer count: more downstream consumers → more canonical
      - Upstream position: sources score higher than derived copies
    """
    consumer_counts = (lineage or {}).get("consumer_counts", {})
    downstream_map = (lineage or {}).get("downstream_map", {})

    scores: dict[str, float] = {}

    for t in tables:
        s = 0.0

        # ── Column completeness (0-10) ────────────────────────────────
        all_col_counts = [len(tt.columns) for tt in tables]
        max_cols = max(all_col_counts) if all_col_counts else 1
        if max_cols > 0:
            s += (len(t.columns) / max_cols) * 10

        # ── Freshness (0-10) ──────────────────────────────────────────
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

        # ── Consumer count (0-10) — lineage-enhanced ──────────────────
        if consumer_counts:
            my_consumers = consumer_counts.get(t.full_name.lower(), 0)
            group_consumers = [
                consumer_counts.get(tt.full_name.lower(), 0) for tt in tables
            ]
            max_consumers = max(group_consumers) if group_consumers else 1
            if max_consumers > 0:
                s += (my_consumers / max_consumers) * 10

        # ── Upstream position (0-10) — lineage-enhanced ───────────────
        if downstream_map:
            other_names = {tt.full_name.lower() for tt in tables if tt != t}
            my_downstream = downstream_map.get(t.full_name.lower(), set())
            feeds_group_members = my_downstream & other_names
            if feeds_group_members:
                s += 10  # this table is upstream of others in the group

        scores[t.full_name] = round(s, 1)

    return scores
