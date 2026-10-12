from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable
import json

from .identity import edge_id, raw_reference_id
from .models import Edge, Node, ParsedFile, ParsedReference, RawReference

# Bump when reference metadata semantics change: rows whose metadata lacks
# the current version are re-derived rather than carried, so full and
# incremental index runs converge on identical classification.
CALL_KIND_VERSION = 1


@dataclass(slots=True)
class ResolutionBatch:
    edges: list[Edge]
    references: list[RawReference]
    reprocessed: int
    reused: int
    # Ids whose persisted row has to be deleted before the new rows go in:
    # everything the resolver was asked to re-derive. Rows not in here were
    # carried in place and are not rewritten at all (stage 7A-2).
    rederive_ids: set[str]
    # Status tally of the carried rows, so the caller can report the table's
    # three-state split without reading the table back.
    carried_status: dict[str, int]


class ReferenceResolver:
    """Resolve parser facts without coupling language grammar logic to graph assembly."""

    def resolve(
        self,
        *,
        parsed_files: dict[str, ParsedFile],
        file_nodes: dict[str, Node],
        symbol_nodes: list[Node],
        generation: int,
        changed_file_ids: set[str] | None = None,
        affected_reference_ids: set[str] | None = None,
        survivor_rows: Iterable[Any] | None = None,
        rederive_reference_ids: set[str] | None = None,
    ) -> ResolutionBatch:
        """Derive the references of changed files, carry the rest in place.

        `survivor_rows` and `rederive_reference_ids` travel together: the rows
        the stream yields are already in the database and are left there, so
        only the ids named in `rederive_reference_ids` are derived again. Pass
        neither and every reference is derived -- that is a full index. Pass
        survivors and the resolver never materialises a reusable row, which is
        what stops update memory from tracking the reference table's size
        (stage 7A-2).
        """
        edges: list[Edge] = []
        raw_references: list[RawReference] = []
        reprocessed = 0
        reused = 0
        carried_status = {"resolved": 0, "ambiguous": 0, "unresolved": 0}
        changed_file_ids = changed_file_ids or set()
        affected_reference_ids = affected_reference_ids or set()
        derive_everything = rederive_reference_ids is None
        rederive = set() if derive_everything else set(rederive_reference_ids)
        by_qualified: dict[str, list[Node]] = defaultdict(list)
        by_name: dict[str, list[Node]] = defaultdict(list)
        by_module = {node.qualified_name: node for node in file_nodes.values()}
        by_file_id: dict[str, list[Node]] = defaultdict(list)
        by_include_suffix: dict[str, list[Node]] = defaultdict(list)
        valid_node_ids = {node.id for node in [*symbol_nodes, *file_nodes.values()]}
        for node in symbol_nodes:
            by_name[node.name].append(node)
            by_qualified[node.qualified_name].append(node)
            if node.file_id:
                by_file_id[node.file_id].append(node)
        for path, node in file_nodes.items():
            parts = path.replace("\\", "/").split("/")
            for index in range(len(parts)):
                by_include_suffix["/".join(parts[index:])].append(node)

        if survivor_rows is not None:
            # One pass, one row at a time: a row that still holds is counted and
            # turned into its edge; a row that does not is named for re-derivation
            # (its candidate set lost a symbol, or its resolution did) and left
            # for the loop below, which is the only place that writes rows.
            for row in survivor_rows:
                if not _survives(row, changed_file_ids=changed_file_ids,
                                 affected_reference_ids=affected_reference_ids,
                                 valid_node_ids=valid_node_ids):
                    rederive.add(row["id"])
                    continue
                reused += 1
                carried_status[row["status"]] += 1
                edge = self._edge_from_row(row, generation)
                if edge:
                    edges.append(edge)

        for relative_path, parsed in parsed_files.items():
            file_node = file_nodes[relative_path]
            file_changed = file_node.id in changed_file_ids
            sources = {node.qualified_name: node for node in by_file_id.get(file_node.id, [])}
            for index, imported in enumerate(parsed.imports):
                source = sources.get(imported.source_qualified_name, file_node)
                module = self._absolute_module(file_node.qualified_name, imported.module)
                if not file_changed and not derive_everything and raw_reference_id(
                    file_node.repo_id, file_node.id, source.id, "IMPORTS",
                    imported.target, index,
                ) not in rederive:
                    continue
                candidates = [item for item in (by_module.get(module),) if item is not None]
                candidates.extend(by_qualified.get(f"{module}.{imported.target}", []))
                strategy = "import_resolution"
                confidence = 1.0
                if not candidates and imported.metadata.get("syntax") == "preproc_include":
                    include_target = imported.target.replace("\\", "/").lstrip("./")
                    matches = by_include_suffix.get(include_target, [])
                    candidates = matches
                    strategy = "include_resolution"
                    confidence = 1.0 if any(
                        node.metadata.get("relative_path") == include_target for node in matches
                    ) else 0.9
                selected = self._select_candidate(candidates)
                record = self._record(
                    repo_id=file_node.repo_id, file_node=file_node, source=source,
                    raw_name=imported.target, reference_type="IMPORTS", target_module=module,
                    start_line=imported.metadata.get("start_line"),
                    end_line=imported.metadata.get("end_line"), ordinal=index,
                    candidates=candidates, strategy=strategy, confidence=confidence,
                    selected=selected, generation=generation,
                )
                raw_references.append(record)
                reprocessed += 1
                edge = self._edge_from_record(record, source.id, generation)
                if edge:
                    edges.append(edge)
            for index, reference in enumerate(parsed.references):
                source = sources.get(reference.source_qualified_name, file_node)
                ordinal = index + len(parsed.imports)
                if not file_changed and not derive_everything and raw_reference_id(
                    file_node.repo_id, file_node.id, source.id, reference.type,
                    reference.target, ordinal,
                ) not in rederive:
                    continue
                candidates, selected, strategy, confidence = self._resolve_reference(
                    reference, source, file_node.qualified_name, parsed, by_qualified, by_name
                )
                record = self._record(
                    repo_id=file_node.repo_id, file_node=file_node, source=source,
                    raw_name=reference.target, reference_type=reference.type,
                    target_module=reference.target_module,
                    start_line=reference.start_line, end_line=reference.end_line,
                    ordinal=ordinal, candidates=candidates,
                    selected=selected, strategy=strategy, confidence=confidence,
                    generation=generation,
                    metadata=reference.metadata,
                )
                raw_references.append(record)
                reprocessed += 1
                edge = self._edge_from_record(record, source.id, generation)
                if edge:
                    edges.append(edge)
        return ResolutionBatch(
            list({edge.id: edge for edge in edges}.values()), raw_references, reprocessed,
            reused, rederive, carried_status,
        )

    def _resolve_reference(
        self,
        reference: ParsedReference,
        source: Node,
        source_module: str,
        parsed_file: ParsedFile,
        by_qualified: dict[str, list[Node]],
        by_name: dict[str, list[Node]],
    ) -> tuple[list[Node], Node | None, str, float]:
        if (reference.metadata or {}).get("call_kind") == "indirect":
            # Indirect calls are never name-bound: a member callback, a
            # function-pointer variable or a complex callee expression must
            # not reach a same-named function.
            return [], None, "indirect_call_shape", 0.0
        target = reference.target.strip(".")
        short = target.rsplit(".", 1)[-1]
        same_file = [candidate for candidate in by_name.get(short, []) if candidate.file_id == source.file_id]
        if len(same_file) == 1:
            return same_file, same_file[0], "same_file_resolution", 0.98
        if len(same_file) > 1:
            return same_file, None, "same_file_resolution", 0.0
        qualified_candidates = by_qualified.get(target, [])
        if qualified_candidates:
            selected = self._select_candidate(qualified_candidates)
            return qualified_candidates, selected, "qualified_name_resolution", 0.99 if selected else 0.0
        if reference.target_module:
            module = self._absolute_module(source_module, reference.target_module)
            qualified = ".".join(part for part in (module, target) if part)
            candidates = by_qualified.get(qualified, [])
            if candidates:
                selected = self._select_candidate(candidates)
                return candidates, selected, "import_resolution", 1.0 if selected else 0.0
        imported_candidates: list[Node] = []
        for imported in parsed_file.imports:
            local = imported.alias or imported.target
            if local == target or local == short:
                module = self._absolute_module(source_module, imported.module)
                imported_candidates.extend(by_qualified.get(f"{module}.{imported.target}", []))
        imported_candidates = list({candidate.id: candidate for candidate in imported_candidates}.values())
        if len(imported_candidates) == 1:
            return imported_candidates, imported_candidates[0], "unique_imported_symbol_resolution", 0.95
        if len(imported_candidates) > 1:
            return imported_candidates, None, "unique_imported_symbol_resolution", 0.0
        candidates = by_name.get(short, [])
        if len(candidates) == 1:
            return candidates, candidates[0], "unique_symbol_resolution", 0.75
        return candidates, None, "ambiguous" if candidates else "unresolved", 0.0

    @staticmethod
    def _select_candidate(candidates: list[Node]) -> Node | None:
        unique = list({candidate.id: candidate for candidate in candidates}.values())
        if len(unique) == 1:
            return unique[0]
        signatures = {candidate.signature for candidate in unique}
        definitions = [candidate for candidate in unique if candidate.metadata.get("definition")]
        if len(signatures) == 1 and len(definitions) == 1:
            return definitions[0]
        return None

    def _record(
        self, *, repo_id: str, file_node: Node, source: Node, raw_name: str,
        reference_type: str, target_module: str | None, start_line: int | None,
        end_line: int | None, ordinal: int, candidates: list[Node], strategy: str,
        confidence: float, generation: int, selected: Node | None = None,
        metadata: dict | None = None,
    ) -> RawReference:
        reference_id = raw_reference_id(
            repo_id, file_node.id, source.id, reference_type, raw_name, ordinal
        )
        candidate_ids = sorted(node.id for node in candidates if node.id != source.id)
        if selected is not None and selected.id == source.id:
            selected = None
            candidate_ids = []
        status = "resolved" if selected else ("ambiguous" if candidate_ids else "unresolved")
        provenance = strategy if status == "resolved" else "tree_sitter_syntax"
        return RawReference(
            reference_id, repo_id, file_node.id, source.id, raw_name, reference_type,
            target_module, start_line, end_line, status, candidate_ids,
            selected.id if selected else None, strategy, provenance,
            confidence if selected else 0.0, generation,
            {"call_kind_version": CALL_KIND_VERSION, **(metadata or {})},
        )

    @staticmethod
    def _absolute_module(source_module: str, module: str) -> str:
        if not module.startswith("."):
            return module
        level = len(module) - len(module.lstrip("."))
        suffix = module[level:]
        package = source_module.split(".")[:-1]
        if level > 1:
            package = package[: -(level - 1)] if level - 1 <= len(package) else []
        return ".".join([*package, *([suffix] if suffix else [])])

    @staticmethod
    def _edge_from_row(row: Any, generation: int) -> Edge | None:
        """The edge of a carried reference, read straight off its stored row.

        Kept next to `_edge_from_record` on purpose: a carried row is not
        rewritten, so its edge is the only thing this pass rebuilds, and it has
        to come out field-for-field what that method would have built. The two
        are pinned equal by `test_carried_edge_matches_the_rebuilt_one`.
        """
        if row["status"] != "resolved" or not row["resolved_symbol_id"]:
            return None
        return Edge(
            edge_id(row["owner_symbol_id"], row["resolved_symbol_id"], row["reference_type"]),
            row["owner_symbol_id"], row["resolved_symbol_id"], row["reference_type"],
            provenance=row["provenance"], confidence=row["confidence"], generation=generation,
            metadata={"raw_reference_id": row["id"], "resolution": row["resolution_strategy"]},
        )

    @staticmethod
    def _edge_from_record(
        record: RawReference,
        source_id: str,
        generation: int,
    ) -> Edge | None:
        if record.status != "resolved" or not record.resolved_symbol_id:
            return None
        return Edge(
            edge_id(source_id, record.resolved_symbol_id, record.reference_type),
            source_id, record.resolved_symbol_id, record.reference_type,
            provenance=record.provenance, confidence=record.confidence, generation=generation,
            metadata={"raw_reference_id": record.id, "resolution": record.resolution_strategy},
        )


def _survives(
    row: Any,
    *,
    changed_file_ids: set[str],
    affected_reference_ids: set[str],
    valid_node_ids: set[str],
) -> bool:
    """Whether a persisted reference row still holds and may be carried in place.

    A row survives only while every symbol it names still exists: a stored
    candidate set that lost a member, or a resolution that lost its target,
    would otherwise keep reporting ambiguous/unresolved facts that a rebuild
    would no longer produce. The file and id clauses are enforced by the SQL
    that produced the row; they are repeated here so this predicate is the
    whole rule and can be read in one place.
    """
    if row["file_id"] in changed_file_ids or row["id"] in affected_reference_ids:
        return False
    resolved_symbol_id = row["resolved_symbol_id"]
    if resolved_symbol_id is not None and resolved_symbol_id not in valid_node_ids:
        return False
    try:
        metadata = json.loads(row["metadata"] or "{}") or {}
    except (TypeError, ValueError):
        metadata = {}
    if metadata.get("call_kind_version") != CALL_KIND_VERSION:
        return False
    return set(json.loads(row["candidate_symbols"])).issubset(valid_node_ids)
