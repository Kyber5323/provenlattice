from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json

from .identity import edge_id, raw_reference_id
from .models import Edge, Node, ParsedFile, ParsedReference, RawReference

# Bump when reference metadata semantics change: cached rows whose metadata
# lacks the current version are re-resolved so full and incremental index
# runs converge on identical classification.
CALL_KIND_VERSION = 1


@dataclass(slots=True)
class ResolutionBatch:
    edges: list[Edge]
    references: list[RawReference]
    reprocessed: int
    reused: int


class ReferenceResolver:
    """Resolve parser facts without coupling language grammar logic to graph assembly."""

    def resolve(
        self,
        *,
        parsed_files: dict[str, ParsedFile],
        file_nodes: dict[str, Node],
        symbol_nodes: list[Node],
        generation: int,
        cached_references: dict[str, dict] | None = None,
        changed_file_ids: set[str] | None = None,
        affected_reference_ids: set[str] | None = None,
    ) -> ResolutionBatch:
        edges: list[Edge] = []
        raw_references: list[RawReference] = []
        reprocessed = 0
        reused = 0
        cached_references = cached_references or {}
        changed_file_ids = changed_file_ids or set()
        affected_reference_ids = affected_reference_ids or set()
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

        for relative_path, parsed in parsed_files.items():
            file_node = file_nodes[relative_path]
            sources = {node.qualified_name: node for node in by_file_id.get(file_node.id, [])}
            for index, imported in enumerate(parsed.imports):
                source = sources.get(imported.source_qualified_name, file_node)
                module = self._absolute_module(file_node.qualified_name, imported.module)
                cached = self._reuse_record(
                    repo_id=file_node.repo_id, file_node=file_node, source=source,
                    raw_name=imported.target, reference_type="IMPORTS", target_module=module,
                    start_line=imported.metadata.get("start_line"),
                    end_line=imported.metadata.get("end_line"), ordinal=index,
                    generation=generation, cached_references=cached_references,
                    changed_file_ids=changed_file_ids,
                    affected_reference_ids=affected_reference_ids,
                    valid_node_ids=valid_node_ids,
                )
                if cached:
                    raw_references.append(cached)
                    reused += 1
                    edge = self._edge_from_record(cached, source, generation)
                    if edge:
                        edges.append(edge)
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
                record, was_reused = self._record(
                    repo_id=file_node.repo_id, file_node=file_node, source=source,
                    raw_name=imported.target, reference_type="IMPORTS", target_module=module,
                    start_line=imported.metadata.get("start_line"),
                    end_line=imported.metadata.get("end_line"), ordinal=index,
                    candidates=candidates, strategy=strategy, confidence=confidence,
                    selected=selected,
                    generation=generation, cached_references=cached_references,
                    changed_file_ids=changed_file_ids,
                    affected_reference_ids=affected_reference_ids,
                    valid_node_ids=valid_node_ids,
                )
                raw_references.append(record)
                reused += int(was_reused)
                reprocessed += int(not was_reused)
                edge = self._edge_from_record(record, source, generation)
                if edge:
                    edges.append(edge)
            for index, reference in enumerate(parsed.references):
                source = sources.get(reference.source_qualified_name, file_node)
                ordinal = index + len(parsed.imports)
                cached = self._reuse_record(
                    repo_id=file_node.repo_id, file_node=file_node, source=source,
                    raw_name=reference.target, reference_type=reference.type,
                    target_module=reference.target_module,
                    start_line=reference.start_line, end_line=reference.end_line,
                    ordinal=ordinal, generation=generation,
                    cached_references=cached_references,
                    changed_file_ids=changed_file_ids,
                    affected_reference_ids=affected_reference_ids,
                    valid_node_ids=valid_node_ids,
                )
                if cached:
                    raw_references.append(cached)
                    reused += 1
                    edge = self._edge_from_record(cached, source, generation)
                    if edge:
                        edges.append(edge)
                    continue
                candidates, selected, strategy, confidence = self._resolve_reference(
                    reference, source, file_node.qualified_name, parsed, by_qualified, by_name
                )
                record, was_reused = self._record(
                    repo_id=file_node.repo_id, file_node=file_node, source=source,
                    raw_name=reference.target, reference_type=reference.type,
                    target_module=reference.target_module,
                    start_line=reference.start_line, end_line=reference.end_line,
                    ordinal=ordinal, candidates=candidates,
                    selected=selected, strategy=strategy, confidence=confidence,
                    generation=generation, cached_references=cached_references,
                    changed_file_ids=changed_file_ids,
                    affected_reference_ids=affected_reference_ids,
                    valid_node_ids=valid_node_ids,
                    metadata=reference.metadata,
                )
                raw_references.append(record)
                reused += int(was_reused)
                reprocessed += int(not was_reused)
                edge = self._edge_from_record(record, source, generation)
                if edge:
                    edges.append(edge)
        return ResolutionBatch(
            list({edge.id: edge for edge in edges}.values()), raw_references, reprocessed, reused
        )

    @staticmethod
    def _cached_metadata(cached: dict | None) -> dict:
        if not cached:
            return {}
        try:
            return json.loads(cached.get("metadata") or "{}")
        except (TypeError, ValueError):
            return {}

    def _reuse_record(
        self, *, repo_id: str, file_node: Node, source: Node, raw_name: str,
        reference_type: str, target_module: str | None, start_line: int | None,
        end_line: int | None, ordinal: int, generation: int,
        cached_references: dict[str, dict], changed_file_ids: set[str],
        affected_reference_ids: set[str], valid_node_ids: set[str],
    ) -> RawReference | None:
        reference_id = raw_reference_id(
            repo_id, file_node.id, source.id, reference_type, raw_name, ordinal
        )
        cached = cached_references.get(reference_id)
        cached_candidate_ids = (
            set(json.loads(cached["candidate_symbols"])) if cached is not None else set()
        )
        if (
            cached is None
            or file_node.id in changed_file_ids
            or reference_id in affected_reference_ids
            or self._cached_metadata(cached).get("call_kind_version") != CALL_KIND_VERSION
            or (
                cached.get("resolved_symbol_id") is not None
                and cached.get("resolved_symbol_id") not in valid_node_ids
            )
            # A raw-reference cache entry is valid only while every candidate
            # it records still exists.  Reusing a partial candidate set after a
            # symbol deletion would preserve stale ambiguous/unresolved facts.
            or not cached_candidate_ids.issubset(valid_node_ids)
        ):
            return None
        return RawReference(
            reference_id, repo_id, file_node.id, source.id, raw_name, reference_type,
            target_module, start_line, end_line, cached["status"],
            json.loads(cached["candidate_symbols"]),
            cached["resolved_symbol_id"], cached["resolution_strategy"],
            cached["provenance"], float(cached["confidence"]), generation,
            json.loads(cached["metadata"]),
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
        confidence: float, generation: int, cached_references: dict[str, dict],
        changed_file_ids: set[str], affected_reference_ids: set[str],
        valid_node_ids: set[str],
        selected: Node | None = None,
        metadata: dict | None = None,
    ) -> tuple[RawReference, bool]:
        reference_id = raw_reference_id(
            repo_id, file_node.id, source.id, reference_type, raw_name, ordinal
        )
        cached = cached_references.get(reference_id)
        cached_candidate_ids = (
            set(json.loads(cached["candidate_symbols"])) if cached is not None else set()
        )
        can_reuse = (
            cached is not None
            and file_node.id not in changed_file_ids
            and reference_id not in affected_reference_ids
            and self._cached_metadata(cached).get("call_kind_version") == CALL_KIND_VERSION
            and cached_candidate_ids.issubset(valid_node_ids)
            and (
                cached.get("resolved_symbol_id") is None
                or cached.get("resolved_symbol_id") in {node.id for node in candidates}
            )
        )
        if can_reuse:
            candidate_ids = json.loads(cached["candidate_symbols"])
            return RawReference(
                reference_id, repo_id, file_node.id, source.id, raw_name, reference_type,
                target_module, start_line, end_line, cached["status"], candidate_ids,
                cached["resolved_symbol_id"], cached["resolution_strategy"],
                cached["provenance"], float(cached["confidence"]), generation,
                json.loads(cached["metadata"]),
            ), True
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
        ), False

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
    def _edge_from_record(
        record: RawReference,
        source: Node,
        generation: int,
    ) -> Edge | None:
        if record.status != "resolved" or not record.resolved_symbol_id:
            return None
        return Edge(
            edge_id(source.id, record.resolved_symbol_id, record.reference_type),
            source.id, record.resolved_symbol_id, record.reference_type,
            provenance=record.provenance, confidence=record.confidence, generation=generation,
            metadata={"raw_reference_id": record.id, "resolution": record.resolution_strategy},
        )
