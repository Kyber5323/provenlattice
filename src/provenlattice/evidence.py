from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable


EVIDENCE_KINDS = {
    "CODE_DEFINITION", "CALL_RELATION", "REFERENCE", "DEPENDENCY",
    "SHARD_RELATION", "DOCUMENT_SECTION", "CROSS_LAYER_LINK",
    "DEFECT_CANDIDATE",
}
BUNDLE_PROFILES = {
    "generic", "document_to_code", "code_to_document", "module_understanding",
}
EVIDENCE_ROLES = {"PRIMARY", "SUPPORTING", "CONTEXTUAL", "SUPPRESSED_CANDIDATE"}
PREFIXES = {
    "CODE_DEFINITION": "CODE", "CALL_RELATION": "CALL", "REFERENCE": "REF",
    "DEPENDENCY": "DEP", "SHARD_RELATION": "SHARD", "DOCUMENT_SECTION": "DOC",
    "CROSS_LAYER_LINK": "XLINK", "DEFECT_CANDIDATE": "DEFECT",
}


def evidence_id(kind: str, repository: str, source_id: str, relation: str,
                target_id: str, fact_id: str = "") -> str:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence kind: {kind}")
    canonical = json.dumps(
        [repository, kind, source_id, relation, target_id, fact_id],
        ensure_ascii=False, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"E-{PREFIXES[kind]}-{digest[:24]}"


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: str
    source_id: str
    relation: str
    target_id: str
    repository: str
    commit: str
    generation: int
    provenance: str
    confidence: float
    source_path: str | None
    source_range: dict[str, int | None] | None
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls, *, kind: str, source_id: str, relation: str, target_id: str,
        repository: str, commit: str, generation: int, provenance: str,
        confidence: float, source_path: str | None, start_line: int | None,
        end_line: int | None, summary: str, fact_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "Evidence":
        return cls(
            evidence_id(kind, repository, source_id, relation, target_id, fact_id),
            kind, source_id, relation, target_id, repository, commit, generation,
            provenance, float(confidence), source_path,
            ({"start_line": start_line, "end_line": end_line}
             if start_line is not None or end_line is not None else None),
            summary, dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """The row as a plain dict, without `dataclasses.asdict`.

        `asdict` introspects the fields and dispatches per value, and the
        multi-hop queries call this tens of thousands of times per query --
        once per evidence item, and until stage 8 twice over. The shape is
        fixed here, so writing it out is both clearer and about twelve times
        faster. The keys are in declaration order deliberately: `bundle_size`
        is a `json.dumps` with no `sort_keys`, so key order is part of the
        byte count it reports.

        `source_range` and `metadata` are copied one level deep. `asdict`
        copies nested containers all the way down; nothing reads the returned
        dict for anything but serialisation, so one level is enough to keep
        the frozen instance from being aliased at the level callers touch.
        """
        source_range = self.source_range
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source_id": self.source_id,
            "relation": self.relation,
            "target_id": self.target_id,
            "repository": self.repository,
            "commit": self.commit,
            "generation": self.generation,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "source_path": self.source_path,
            "source_range": dict(source_range) if source_range is not None else None,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryBudget:
    max_evidence: int = 20
    max_symbols: int = 8
    max_edges: int = 12
    max_sections: int = 6
    # The legacy fields above continue to describe graph traversal.  These fields
    # describe only what is exposed in a bundle.  None preserves the R2.3 shape.
    max_primary: int | None = None
    max_supporting: int | None = None
    max_total: int | None = None

    def __post_init__(self) -> None:
        values = (self.max_evidence, self.max_symbols, self.max_edges, self.max_sections,
                  *(value for value in (self.max_primary, self.max_supporting, self.max_total)
                    if value is not None))
        if min(values) < 0:
            raise ValueError("query budgets must be >= 0")

    @property
    def total_limit(self) -> int:
        return self.max_evidence if self.max_total is None else min(self.max_evidence, self.max_total)

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


def _profile_kind_rank(profile: str, kind: str) -> int:
    # These are intentionally query-class weights, not global evidence quality.
    weights = {
        "document_to_code": {"CROSS_LAYER_LINK": 5, "DOCUMENT_SECTION": 4,
                             "CODE_DEFINITION": 3, "CALL_RELATION": 2,
                             "SHARD_RELATION": 1},
        "code_to_document": {"DOCUMENT_SECTION": 5, "CROSS_LAYER_LINK": 4,
                             "CODE_DEFINITION": 3, "SHARD_RELATION": 2},
        "module_understanding": {"CODE_DEFINITION": 5, "CROSS_LAYER_LINK": 4,
                                  "DOCUMENT_SECTION": 3, "DEPENDENCY": 2,
                                  "SHARD_RELATION": 1},
        "generic": {},
    }
    return weights[profile].get(kind, 0)


def _role_for(item: Evidence, profile: str, focus_terms: tuple[str, ...]) -> str:
    metadata = item.metadata
    if profile == "generic":
        return str(metadata.get("bundle_seed_role", "SUPPORTING"))
    if str(metadata.get("resolution_status", "resolved")) != "resolved":
        return "SUPPRESSED_CANDIDATE"
    text = " ".join(filter(None, (item.source_id, item.target_id, item.source_path, item.summary))).casefold()
    focused = not focus_terms or any(term.casefold() in text for term in focus_terms)
    if not focused and profile != "generic":
        return "SUPPRESSED_CANDIDATE"
    primary_kinds = {
        "document_to_code": {"DOCUMENT_SECTION", "CROSS_LAYER_LINK"},
        "code_to_document": {"DOCUMENT_SECTION", "CROSS_LAYER_LINK"},
        "module_understanding": {"CODE_DEFINITION"},
    }
    if item.kind in primary_kinds[profile]:
        return "PRIMARY"
    if item.kind in {"CODE_DEFINITION", "CROSS_LAYER_LINK", "DOCUMENT_SECTION",
                     "CALL_RELATION", "DEPENDENCY"}:
        return "SUPPORTING"
    return "CONTEXTUAL"


def rank_evidence(values: Iterable[Evidence], *, profile: str = "generic",
                  focus_terms: Iterable[str] = ()) -> list[Evidence]:
    if profile not in BUNDLE_PROFILES:
        raise ValueError(f"unsupported bundle profile: {profile}")
    status_rank = {"resolved": 2, "ambiguous": 1, "unresolved": 0}
    normalized_focus = tuple(str(term).strip() for term in focus_terms if str(term).strip())

    # The role is a pure function of the item and the profile, and both the sort
    # key and the annotation below need it. Asking for it in each place did the
    # work twice per item -- 127,552 calls for one 63,776-item subgraph -- so it
    # is computed once here and carried through the sort.
    decorated = [(item, _role_for(item, profile, normalized_focus)) for item in values]

    def key(pair: tuple[Evidence, str]) -> tuple:
        item, role = pair
        metadata = item.metadata
        return (
            -({"PRIMARY": 3, "SUPPORTING": 2, "CONTEXTUAL": 1,
                "SUPPRESSED_CANDIDATE": 0}[role]),
            -_profile_kind_rank(profile, item.kind),
            -status_rank.get(str(metadata.get("resolution_status", "resolved")), 0),
            -int(bool(metadata.get("direct", True))),
            -item.confidence,
            int(metadata.get("distance", 0)),
            -int(bool(metadata.get("boundary_relevant", False))),
            item.evidence_id,
        )

    ranked = sorted(decorated, key=key)
    return [replace(item, metadata={**item.metadata, "bundle_role": role,
                                    "bundle_profile": profile})
            for item, role in ranked]


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    anchor: str
    intent: str
    summary: str
    primary_evidence: tuple[Evidence, ...]
    supporting_evidence: tuple[Evidence, ...]
    related_entities: tuple[dict[str, Any], ...]
    uncertainties: tuple[str, ...]
    provenance: tuple[str, ...]
    generation: int
    budget: QueryBudget
    profile: str = "generic"
    suppressed_evidence_ids: tuple[str, ...] = ()
    fallback_triggered: bool = False

    @classmethod
    def create(
        cls, *, anchor: str, intent: str, summary: str,
        primary_evidence: Iterable[Evidence], supporting_evidence: Iterable[Evidence],
        related_entities: Iterable[dict[str, Any]], uncertainties: Iterable[str],
        generation: int, budget: QueryBudget,
        profile: str = "generic", focus_terms: Iterable[str] = (),
    ) -> "EvidenceBundle":
        if profile not in BUNDLE_PROFILES:
            raise ValueError(f"unsupported bundle profile: {profile}")
        candidates: dict[str, Evidence] = {}
        for seed_role, values in (("PRIMARY", primary_evidence), ("SUPPORTING", supporting_evidence)):
            for item in values:
                candidates.setdefault(item.evidence_id, replace(
                    item, metadata={**item.metadata, "bundle_seed_role": seed_role}))
        ranked = rank_evidence(candidates.values(), profile=profile, focus_terms=focus_terms)
        suppressed = [item for item in ranked
                      if item.metadata.get("bundle_role") == "SUPPRESSED_CANDIDATE"]
        eligible = [item for item in ranked if item not in suppressed]
        # Focus terms are optional task hints and must never turn a non-empty
        # query into an empty bundle.  If none match, fall back deterministically
        # to contextual evidence rather than silently dropping all coverage.
        fallback_triggered = False
        if not eligible and suppressed:
            fallback_triggered = True
            eligible = [replace(item, metadata={**item.metadata,
                                                "bundle_role": "CONTEXTUAL"})
                        for item in suppressed]
            suppressed = []
        primary = [item for item in eligible if item.metadata.get("bundle_role") == "PRIMARY"]
        supporting = [item for item in eligible if item.metadata.get("bundle_role") != "PRIMARY"]
        if fallback_triggered:
            # A zero-hit focus hint is advisory.  Preserve the full frozen candidate
            # set rather than applying a budget whose arbitrary truncation could lose
            # evidence coverage; qualification reports this expansion explicitly.
            selected_primary, selected_supporting = [], eligible
        else:
            primary_limit = budget.total_limit if budget.max_primary is None else budget.max_primary
            supporting_limit = budget.total_limit if budget.max_supporting is None else budget.max_supporting
            selected_primary = primary[:primary_limit]
            remaining = max(budget.total_limit - len(selected_primary), 0)
            selected_supporting = supporting[:min(supporting_limit, remaining)]
        combined = [*selected_primary, *selected_supporting]
        return cls(
            anchor, intent, summary, tuple(selected_primary), tuple(selected_supporting),
            tuple(dict(item) for item in list(related_entities)[:budget.max_symbols]),
            tuple(uncertainties), tuple(sorted({item.provenance for item in combined})),
            generation, budget, profile, tuple(item.evidence_id for item in suppressed),
            fallback_triggered,
        )

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in (*self.primary_evidence, *self.supporting_evidence)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor, "intent": self.intent, "summary": self.summary,
            "primary_evidence": [item.to_dict() for item in self.primary_evidence],
            "supporting_evidence": [item.to_dict() for item in self.supporting_evidence],
            "related_entities": list(self.related_entities),
            "uncertainties": list(self.uncertainties), "provenance": list(self.provenance),
            "generation": self.generation, "budget": self.budget.to_dict(),
            "profile": self.profile,
            "suppressed_evidence_ids": list(self.suppressed_evidence_ids),
            "suppressed_evidence_count": len(self.suppressed_evidence_ids),
            "fallback_triggered": self.fallback_triggered,
            "returned_evidence_ids": self.evidence_ids,
            "returned_evidence_count": len(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class DefectEvidenceBundle:
    """One defect candidate's evidence, in the shape design doc §15 froze.

    The first eight members are §15's verbatim -- `facts` are what the graph
    proved, `uncertain_facts` what it could not, and `missing_evidence` names
    what a stronger analysis would have to add. The bookkeeping keys follow,
    because a reader comparing a bundle against §15 should meet the same block
    first.

    This is a parallel type, not a subclass of `EvidenceBundle`. That one is a
    *retrieval* bundle: it ranks and truncates heterogeneous evidence under a
    `QueryBudget`. Here the evidence is one candidate and its path, the
    truncation already happened inside the query (`max_candidates`,
    `max_paths`), and inheriting would force §15's members into fields that
    mean something else.
    """

    defect_type: str
    defect_key: str
    subject: dict[str, Any]
    facts: tuple[dict[str, Any], ...]
    uncertain_facts: tuple[dict[str, Any], ...]
    paths: tuple[dict[str, Any], ...]
    source_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    candidate_id: str
    anchor: str
    resolution_status: str
    confidence: float
    generation: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-native throughout: this goes straight into a query result's
        `data` and out through the CLI's `json.dumps` without a second pass."""
        return {
            "defect_type": self.defect_type,
            "defect_key": self.defect_key,
            "subject": dict(self.subject),
            "facts": [dict(fact) for fact in self.facts],
            "uncertain_facts": [dict(fact) for fact in self.uncertain_facts],
            "paths": [dict(path) for path in self.paths],
            "source_evidence": list(self.source_evidence),
            "missing_evidence": list(self.missing_evidence),
            "candidate_id": self.candidate_id,
            "anchor": self.anchor,
            "resolution_status": self.resolution_status,
            "confidence": self.confidence,
            "generation": self.generation,
        }


def parse_evidence_citations(text: str) -> list[str]:
    import re
    pattern = r"\bE-(?:CODE|CALL|REF|DEP|SHARD|DOC|XLINK|DEFECT)-[0-9a-f]{24}\b"
    return list(dict.fromkeys(re.findall(pattern, text)))
