from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from .analysis_query import ANALYSIS_RESPONSE_NODE_LIMIT, query_analysis
from .evidence import Evidence, EvidenceBundle, QueryBudget, evidence_id, rank_evidence
from .orchestrator import AnalysisRequestError as _AnalysisRequestError
from .overlay import GraphView
from .storage import decode_row


SYMBOL_KINDS = ("Namespace", "Class", "Struct", "Interface", "Function", "Method", "Type")
CROSS_RELATIONS = {"DESCRIBES", "CONSTRAINS", "IMPLEMENTED_BY", "VERIFIED_BY",
                   "BELONGS_TO", "DOCUMENTS"}


def _parse_analysis_request(analysis):
    if analysis is None:
        return None
    if isinstance(analysis, str):
        names = [item.strip() for item in analysis.split(",") if item.strip()]
    else:
        names = [str(item).strip() for item in analysis if str(item).strip()]
    names = [name for name in names if name != "none"]
    if not names:
        return None
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique


def _count_tree(node) -> int:
    return 1 + sum(_count_tree(child) for child in node.get("children", []))


def _prune_tree(node, limit: int) -> tuple[dict, int]:
    kept = 1
    emitted = dict(node)
    children = []
    for child in node.get("children", []):
        if kept >= limit:
            break
        pruned, child_kept = _prune_tree(child, limit - kept)
        children.append(pruned)
        kept += child_kept
    emitted["children"] = children
    return emitted, kept


class GraphQuery:
    def __init__(
        self, database: str | Path, *, branch_overlay: str | Path | None = None,
        session_overlay: str | Path | None = None, current_base_commit: str | None = None,
    ) -> None:
        self.view = GraphView(
            database, branch_overlay, session_overlay,
            current_base_commit=current_base_commit,
        )
        self.storage = self.view.base
        self._evidence_context: tuple[str, str] | None = None

    def close(self) -> None:
        self.view.close()

    def __enter__(self) -> "GraphQuery":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def graph_generation(self) -> int:
        return self.view.graph_generation

    def _result(self, data: object, started: float, evidence: list[Evidence] | None = None,
                *, query_type: str = "query", anchor: str = "") -> dict:
        evidence = rank_evidence(evidence or [])
        query_seed = json.dumps([query_type, anchor, self.graph_generation], separators=(",", ":"))
        return {"graph_generation": self.graph_generation,
                "query_time_ms": (time.perf_counter() - started) * 1000, "data": data,
                "query_id": f"Q-{hashlib.sha256(query_seed.encode()).hexdigest()[:20]}",
                "query_type": query_type, "anchor": anchor,
                "evidence": [item.to_dict() for item in evidence],
                "returned_evidence_ids": [item.evidence_id for item in evidence],
                "returned_evidence_count": len(evidence),
                "bundle_size": len(json.dumps([item.to_dict() for item in evidence],
                                               ensure_ascii=False))}

    def _context(self) -> tuple[str, str]:
        if self._evidence_context is not None:
            return self._evidence_context
        repository = decode_row(self.storage.repository()) if self.storage.repository() else {}
        repository_id = str(repository.get("repo_id") or repository.get("name") or "unknown")
        metadata = repository.get("metadata") or {}
        commit = str(metadata.get("commit") or "")
        if not commit and repository.get("root_path"):
            try:
                root_path = str(repository["root_path"])
                completed = subprocess.run(
                    ["git", "-c", f"safe.directory={root_path}", "-C", root_path,
                     "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if completed.returncode == 0:
                    commit = completed.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        self._evidence_context = (repository_id, commit or "unknown")
        return self._evidence_context

    def _source_path(self, node: dict | None) -> str | None:
        if not node:
            return None
        metadata = node.get("metadata") or {}
        if metadata.get("path"):
            return str(metadata["path"])
        if node.get("file_id"):
            row = self.storage.row("SELECT path FROM files WHERE file_id=?", (node["file_id"],))
            if row:
                return str(row["path"])
        return None

    def _node_evidence(self, node: dict) -> Evidence:
        repository, commit = self._context()
        is_document = node.get("kind") in {"Document", "DocumentSection", "Requirement",
                                                   "Spec", "ShardDocument"}
        kind = "DOCUMENT_SECTION" if is_document else "CODE_DEFINITION"
        relation = "DOCUMENTS" if is_document else "DEFINES"
        return Evidence.create(
            kind=kind, source_id=str(node.get("file_id") or node["id"]), relation=relation,
            target_id=node["id"], repository=repository, commit=commit,
            generation=int(node.get("generation") or self.graph_generation),
            provenance=("markdown_structure" if is_document else "tree_sitter_syntax"),
            confidence=1.0, source_path=self._source_path(node),
            start_line=node.get("start_line"), end_line=node.get("end_line"),
            summary=f"{node.get('kind')} {node.get('qualified_name') or node.get('name')}",
            fact_id=node["id"], metadata={"entity_kind": node.get("kind"), "direct": True},
        )

    def _edge_evidence(self, edge: dict, nodes: dict[str, dict], *, distance: int = 1) -> Evidence:
        repository, commit = self._context()
        source, target = nodes.get(edge["src_id"]), nodes.get(edge["dst_id"])
        relation = edge["type"]
        if relation == "CALLS":
            kind = "CALL_RELATION"
        elif relation == "REFERENCES":
            kind = "REFERENCE"
        elif relation in CROSS_RELATIONS:
            kind = "CROSS_LAYER_LINK"
        else:
            kind = "DEPENDENCY"
        metadata = dict(edge.get("metadata") or {})
        metadata.update({"fact_id": edge["id"], "direct": distance <= 1, "distance": distance,
                         "resolution_status": "resolved"})
        return Evidence.create(
            kind=kind, source_id=edge["src_id"], relation=relation, target_id=edge["dst_id"],
            repository=repository, commit=commit,
            generation=int(edge.get("generation") or self.graph_generation),
            provenance=str(edge.get("provenance") or "static_analysis"),
            confidence=float(edge.get("confidence") or 0.0), source_path=self._source_path(source),
            start_line=(source or {}).get("start_line"), end_line=(source or {}).get("end_line"),
            summary=(f"{(source or {}).get('qualified_name', edge['src_id'])} {relation} "
                     f"{(target or {}).get('qualified_name', edge['dst_id'])}"),
            fact_id=edge["id"], metadata=metadata,
        )

    def _raw_reference_evidence(self, row: dict) -> Evidence:
        repository, commit = self._context()
        target = row.get("resolved_symbol_id") or next(iter(row.get("candidate_symbols") or []),
                                                        f"raw:{row.get('raw_name', '')}")
        owner = self.view.by_ids("Node", [row.get("owner_symbol_id")])
        source = decode_row(owner[0]) if owner else None
        return Evidence.create(
            kind="REFERENCE", source_id=row["owner_symbol_id"],
            relation=row.get("reference_type") or "REFERENCES", target_id=target,
            repository=repository, commit=commit,
            generation=int(row.get("generation") or self.graph_generation),
            provenance=str(row.get("provenance") or "tree_sitter_syntax"),
            confidence=float(row.get("confidence") or 0.0),
            source_path=self._source_path(source), start_line=row.get("start_line"),
            end_line=row.get("end_line"), summary=f"reference {row.get('raw_name')}",
            fact_id=row["id"], metadata={"fact_id": row["id"],
                "resolution_status": row.get("status"),
                "candidate_symbols": row.get("candidate_symbols") or [], "direct": True},
        )

    def _raw_link_evidence(self, row: dict) -> Evidence:
        repository, commit = self._context()
        targets = row.get("candidate_targets") or []
        target = row.get("resolved_target_id") or next(iter(targets), f"raw:{row.get('raw_anchor', '')}")
        source_rows = self.view.by_ids("Node", [row["source_node_id"]])
        source = decode_row(source_rows[0]) if source_rows else None
        return Evidence.create(
            kind="CROSS_LAYER_LINK", source_id=row["source_node_id"],
            relation="CANDIDATE" if row.get("resolution_status") != "resolved" else "DOCUMENTS",
            target_id=target, repository=repository, commit=commit,
            generation=int(row.get("generation") or self.graph_generation),
            provenance=str(row.get("provenance") or "document_anchor"),
            confidence=float(row.get("confidence") or 0.0), source_path=self._source_path(source),
            start_line=(source or {}).get("start_line"), end_line=(source or {}).get("end_line"),
            summary=f"{row.get('resolution_status')} cross-layer anchor {row.get('raw_anchor')}",
            fact_id=row["id"], metadata={"fact_id": row["id"],
                "resolution_status": row.get("resolution_status"), "candidate_targets": targets,
                "resolution_strategy": row.get("resolution_strategy"), "direct": True},
        )

    def find_symbol(self, name: str) -> dict:
        started = time.perf_counter()
        placeholders = ",".join("?" for _ in SYMBOL_KINDS)

        def matches(row: dict) -> bool:
            qualified = row.get("qualified_name", "")
            return row.get("kind") in SYMBOL_KINDS and (
                row.get("name") == name or qualified == name or qualified.endswith(f".{name}")
            )

        rows = self.view.query(
            "Node",
            f"SELECT * FROM nodes WHERE kind IN ({placeholders}) "
            "AND (name=? OR qualified_name=? OR qualified_name LIKE ?)",
            (*SYMBOL_KINDS, name, name, f"%.{name}"), matches,
        )
        rows.sort(key=lambda item: (item["qualified_name"], item["id"]))
        data = [decode_row(row) for row in rows]
        return self._result(data, started, [self._node_evidence(row) for row in data],
                            query_type="symbol", anchor=name)

    def _resolve(self, symbol: str) -> dict | None:
        matches = lambda row: (
            row.get("id") == symbol or row.get("qualified_name") == symbol
            or row.get("name") == symbol
        )
        rows = self.view.query(
            "Node", "SELECT * FROM nodes WHERE id=? OR qualified_name=? OR name=?",
            (symbol, symbol, symbol), matches,
        )
        if not rows:
            return None
        return min(rows, key=lambda row: (
            0 if row["id"] == symbol else 1 if row["qualified_name"] == symbol else 2,
            row["qualified_name"], row["id"],
        ))

    def get_definition(self, symbol: str) -> dict:
        started = time.perf_counter()
        row = self._resolve(symbol)
        data = decode_row(row) if row else None
        return self._result(data, started, [self._node_evidence(data)] if data else [],
                            query_type="definition", anchor=symbol)

    def _neighbors(self, symbol: str, edge_type: str, incoming: bool) -> dict:
        started = time.perf_counter()
        anchor = self._resolve(symbol)
        if not anchor:
            return self._result([], started)
        edge_column, node_column = ("dst_id", "src_id") if incoming else ("src_id", "dst_id")
        edges = self.view.query(
            "Edge", f"SELECT * FROM edges WHERE {edge_column}=? AND type=?",
            (anchor["id"], edge_type),
            lambda row: row.get(edge_column) == anchor["id"] and row.get("type") == edge_type,
        )
        nodes = {row["id"]: row for row in self.view.by_ids(
            "Node", (edge[node_column] for edge in edges)
        )}
        result = []
        evidence = []
        decoded_anchor = decode_row(anchor)
        decoded_nodes = {node_id: decode_row(node) for node_id, node in nodes.items()}
        evidence_nodes = {anchor["id"]: decoded_anchor, **decoded_nodes}
        for edge in edges:
            node = nodes.get(edge[node_column])
            if node is None:
                continue
            item = dict(node)
            item["relation"], item["edge_metadata"] = edge["type"], edge["metadata"]
            result.append(decode_row(item))
            evidence.append(self._edge_evidence(decode_row(edge), evidence_nodes))
        result.sort(key=lambda item: (item["qualified_name"], item["id"]))
        direction = "callers" if edge_type == "CALLS" and incoming else (
            "callees" if edge_type == "CALLS" else "references")
        return self._result(result, started, evidence, query_type=direction, anchor=symbol)

    def get_callers(self, symbol: str) -> dict:
        return self._neighbors(symbol, "CALLS", True)

    def get_callees(self, symbol: str) -> dict:
        return self._neighbors(symbol, "CALLS", False)

    def get_references(self, symbol: str) -> dict:
        return self._neighbors(symbol, "REFERENCES", True)

    def get_raw_references(
        self, *, raw_name: str | None = None, file_id: str | None = None,
        owner_symbol_id: str | None = None, resolved_symbol_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        started = time.perf_counter()
        filters = {"raw_name": raw_name, "file_id": file_id,
                   "owner_symbol_id": owner_symbol_id,
                   "resolved_symbol_id": resolved_symbol_id, "status": status}
        active = {key: value for key, value in filters.items() if value is not None}
        where = " AND ".join(f"{key}=?" for key in active)
        sql = "SELECT * FROM raw_references" + (f" WHERE {where}" if where else "")
        rows = self.view.query(
            "RawReference", sql, tuple(active.values()),
            lambda row: all(row.get(key) == value for key, value in active.items()),
        )
        rows.sort(key=lambda row: (row["file_id"], row.get("start_line") or 0, row["id"]))
        data = [decode_row(row) for row in rows]
        return self._result(data, started, [self._raw_reference_evidence(row) for row in data],
                            query_type="raw_references", anchor=raw_name or resolved_symbol_id or "")

    def get_evidence_links(
        self, *, source_node_id: str | None = None,
        resolved_target_id: str | None = None, status: str | None = None,
        anchor_type: str | None = None,
    ) -> dict:
        started = time.perf_counter()
        filters = {"source_node_id": source_node_id, "resolved_target_id": resolved_target_id,
                   "resolution_status": status, "anchor_type": anchor_type}
        active = {key: value for key, value in filters.items() if value is not None}
        where = " AND ".join(f"{key}=?" for key in active)
        sql = "SELECT * FROM raw_evidence_links" + (f" WHERE {where}" if where else "")
        rows = self.view.query(
            "RawEvidenceLink", sql, tuple(active.values()),
            lambda row: all(row.get(key) == value for key, value in active.items()),
        )
        rows.sort(key=lambda row: (row["source_node_id"], row["raw_anchor"], row["id"]))
        data = [decode_row(row) for row in rows]
        return self._result(data, started, [self._raw_link_evidence(row) for row in data],
                            query_type="cross_layer_evidence",
                            anchor=source_node_id or resolved_target_id or status or "")

    def _cross_layer_neighbors(self, anchor: str, relations: set[str], incoming: bool) -> dict:
        started = time.perf_counter()
        node = self._resolve(anchor)
        if not node:
            return self._result([], started)
        edge_column, node_column = (("dst_id", "src_id") if incoming
                                    else ("src_id", "dst_id"))
        ordered_relations = sorted(relations)
        placeholders = ",".join("?" for _ in ordered_relations)
        edges = self.view.query(
            "Edge", f"SELECT * FROM edges WHERE {edge_column}=? AND type IN ({placeholders})",
            (node["id"], *ordered_relations),
            lambda row: row.get(edge_column) == node["id"] and row.get("type") in relations,
        )
        targets = {row["id"]: row for row in self.view.by_ids(
            "Node", (edge[node_column] for edge in edges)
        )}
        result = []
        evidence = []
        decoded_node = decode_row(node)
        decoded_targets = {node_id: decode_row(target) for node_id, target in targets.items()}
        evidence_nodes = {node["id"]: decoded_node, **decoded_targets}
        for edge in edges:
            target = targets.get(edge[node_column])
            if target:
                item = decode_row(target)
                item["relation"] = edge["type"]
                item["edge_metadata"] = decode_row(edge).get("metadata", {})
                result.append(item)
                evidence.append(self._edge_evidence(decode_row(edge), evidence_nodes))
        result.sort(key=lambda item: (item["qualified_name"], item["id"]))
        return self._result(result, started, evidence, query_type="cross_layer",
                            anchor=anchor)

    def get_implemented_code(self, requirement: str) -> dict:
        return self._cross_layer_neighbors(requirement, {"IMPLEMENTED_BY"}, False)

    def get_requirements(self, symbol: str) -> dict:
        result = self._cross_layer_neighbors(symbol, {"IMPLEMENTED_BY"}, True)
        result["data"] = [item for item in result["data"] if item.get("kind") == "Requirement"]
        return result

    def get_document_targets(self, document: str) -> dict:
        return self._cross_layer_neighbors(
            document, {"DESCRIBES", "CONSTRAINS", "IMPLEMENTED_BY", "VERIFIED_BY",
                       "BELONGS_TO", "DOCUMENTS"}, False,
        )

    def _shard(self, shard: str) -> dict | None:
        rows = self.view.query(
            "Shard", "SELECT * FROM shards WHERE shard_id=? OR path=?", (shard, shard),
            lambda row: row.get("shard_id") == shard or row.get("path") == shard,
        )
        return decode_row(min(rows, key=lambda row: (row["path"], row["shard_id"]))) if rows else None

    def _shard_edge_evidence(self, edge: dict, source_path: str | None = None) -> Evidence:
        repository, commit = self._context()
        return Evidence.create(
            kind="SHARD_RELATION", source_id=edge["src_shard_id"],
            relation=edge["edge_type"], target_id=edge["dst_shard_id"],
            repository=repository, commit=commit, generation=self.graph_generation,
            provenance="shard_boundary", confidence=1.0, source_path=source_path,
            start_line=None, end_line=None,
            summary=f"{edge['src_shard_id']} {edge['edge_type']} {edge['dst_shard_id']}",
            fact_id=edge["edge_id"], metadata={"fact_id": edge["edge_id"],
                "boundary_relevant": True, "direct": True},
        )

    def _boundary(self, shard: str, dependents: bool) -> dict:
        started = time.perf_counter()
        anchor = self._shard(shard)
        if not anchor:
            return self._result([], started)
        source, target = (("dst_shard_id", "src_shard_id") if dependents
                          else ("src_shard_id", "dst_shard_id"))
        edges = self.view.query(
            "BoundaryEdge", f"SELECT * FROM shard_edges WHERE {source}=?",
            (anchor["shard_id"],), lambda row: row.get(source) == anchor["shard_id"],
        )
        rows = self.view.by_ids("Shard", {edge[target] for edge in edges})
        rows.sort(key=lambda item: (item["path"], item["shard_id"]))
        data = [decode_row(row) for row in rows]
        evidence = [self._shard_edge_evidence(edge, anchor["path"]) for edge in edges]
        return self._result(data, started, evidence,
                            query_type="dependents" if dependents else "dependencies", anchor=shard)

    def get_dependencies(self, shard: str) -> dict:
        return self._boundary(shard, False)

    def get_dependents(self, shard: str) -> dict:
        return self._boundary(shard, True)

    def get_boundary_references(self, symbol: str) -> dict:
        started = time.perf_counter()
        anchor = self._resolve(symbol)
        if not anchor:
            return self._result([], started)
        references = self.view.query(
            "RawReference", "SELECT * FROM raw_references WHERE resolved_symbol_id=?",
            (anchor["id"],), lambda row: row.get("resolved_symbol_id") == anchor["id"],
        )
        node_ids = {value for reference in references for value in (
            reference.get("owner_symbol_id"), reference.get("resolved_symbol_id")) if value}
        nodes = {row["id"]: row for row in self.view.by_ids("Node", node_ids)}
        rows = [reference for reference in references
                if nodes.get(reference["owner_symbol_id"], {}).get("shard_id")
                != nodes.get(reference["resolved_symbol_id"], {}).get("shard_id")]
        rows.sort(key=lambda row: (row["file_id"], row.get("start_line") or 0, row["id"]))
        data = [decode_row(row) for row in rows]
        return self._result(data, started, [self._raw_reference_evidence(row) for row in data],
                            query_type="boundary_references", anchor=symbol)

    def get_subgraph(self, anchor: str, max_hops: int = 2, max_nodes: int = 100) -> dict:
        if max_hops < 0 or max_nodes < 1:
            raise ValueError("max_hops must be >= 0 and max_nodes must be >= 1")
        started = time.perf_counter()
        root = self._resolve(anchor)
        if not root:
            return self._result({"nodes": [], "edges": []}, started)
        seen, frontier = {root["id"]}, {root["id"]}
        selected: dict[str, dict] = {}
        for _ in range(max_hops):
            if not frontier or len(seen) >= max_nodes:
                break
            placeholders = ",".join("?" for _ in frontier)
            values = tuple(sorted(frontier))
            edges = self.view.query(
                "Edge", f"SELECT * FROM edges WHERE src_id IN ({placeholders}) "
                f"OR dst_id IN ({placeholders})", (*values, *values),
                lambda row: row.get("src_id") in frontier or row.get("dst_id") in frontier,
            )
            next_frontier: set[str] = set()
            for edge in sorted(edges, key=lambda row: row["id"]):
                for other in (edge["src_id"], edge["dst_id"]):
                    if other not in seen and len(seen) < max_nodes:
                        seen.add(other)
                        next_frontier.add(other)
                if edge["src_id"] in seen and edge["dst_id"] in seen:
                    selected[edge["id"]] = decode_row(edge)
            frontier = next_frontier
        nodes = [decode_row(row) for row in self.view.by_ids("Node", seen)]
        node_map = {node["id"]: node for node in nodes}
        edge_values = list(selected.values())
        evidence = [self._node_evidence(node) for node in nodes]
        evidence += [self._edge_evidence(edge, node_map) for edge in edge_values]
        return self._result({"nodes": nodes, "edges": edge_values}, started, evidence[:max_nodes],
                            query_type="subgraph", anchor=anchor)

    @staticmethod
    def _budget(max_evidence: int, max_symbols: int, max_edges: int, max_sections: int,
                max_primary: int | None = None, max_supporting: int | None = None,
                max_total: int | None = None) -> QueryBudget:
        return QueryBudget(max_evidence, max_symbols, max_edges, max_sections,
                           max_primary, max_supporting, max_total)

    @staticmethod
    def _evidence_from(result: dict) -> list[Evidence]:
        return [Evidence(**item) for item in result.get("evidence", [])]

    def _bundle_result(self, bundle: EvidenceBundle, started: float) -> dict:
        value = bundle.to_dict()
        seed = json.dumps([bundle.intent, bundle.anchor, bundle.generation], separators=(",", ":"))
        return {"query_id": f"Q-{hashlib.sha256(seed.encode()).hexdigest()[:20]}",
                "query_type": bundle.intent, "anchor": bundle.anchor,
                "graph_generation": self.graph_generation,
                "query_time_ms": (time.perf_counter() - started) * 1000,
                "returned_evidence_ids": value["returned_evidence_ids"],
                "returned_evidence_count": value["returned_evidence_count"],
                "bundle_size": len(json.dumps(value, ensure_ascii=False)), "bundle": value}

    def _analysis_sections(self, anchor_id: str, features) -> dict:
        repository = self.storage.repository()
        root_path = repository["root_path"] if repository else None
        sections = {}
        for feature in features:
            try:
                result = query_analysis(root_path, feature, symbol_id=anchor_id,
                                        database=self.storage.path)
            except _AnalysisRequestError as error:
                sections[feature] = {"status": "unavailable", "reason": error.code,
                                     "coverage": {"type": "none"},
                                     "nodes": [], "edges": []}
                continue
            section = {"status": result["status"], "reason": result["reason"],
                       "coverage": result["coverage"],
                       "nodes": [], "edges": []}
            if result["status"] == "available":
                nodes = result.get("nodes", [])
                edges = result.get("edges", [])
                total_nodes, total_edges = len(nodes), len(edges)
                truncated = False
                if len(nodes) > ANALYSIS_RESPONSE_NODE_LIMIT:
                    truncated = True
                    kept = {node["analysis_node_id"]
                            for node in nodes[:ANALYSIS_RESPONSE_NODE_LIMIT]}
                    nodes = nodes[:ANALYSIS_RESPONSE_NODE_LIMIT]
                    edges = [edge for edge in edges
                             if edge["src_id"] in kept and edge["dst_id"] in kept]
                section["nodes"] = nodes
                section["edges"] = edges
                section["truncated"] = truncated
                section["returned_nodes"] = len(nodes)
                section["total_nodes"] = total_nodes
                section["returned_edges"] = len(edges)
                section["total_edges"] = total_edges
                if result.get("entry_id") is not None:
                    section["entry_id"] = result["entry_id"]
                    section["exit_id"] = result["exit_id"]
                root = result.get("root")
                if root is not None:
                    total_nodes = _count_tree(root)
                    if total_nodes > ANALYSIS_RESPONSE_NODE_LIMIT:
                        truncated = True
                        root, returned = _prune_tree(root, ANALYSIS_RESPONSE_NODE_LIMIT)
                        section["returned_nodes"] = returned
                    else:
                        section["returned_nodes"] = total_nodes
                    section["total_nodes"] = total_nodes
                    section["truncated"] = truncated
                    section["root"] = root
            sections[feature] = section
        return sections

    def _analysis_v1(self, anchor: dict, features) -> dict:
        requested_features = sorted(features)
        return {
            "version": "analysis-v1",
            "requested_features": requested_features,
            "targets": [{
                "symbol_id": anchor["id"],
                "qualified_name": anchor["qualified_name"],
                "features": self._analysis_sections(anchor["id"], requested_features),
            }],
        }

    @staticmethod
    def _analysis_v1_error(features, code: str, message: str) -> dict:
        return {
            "version": "analysis-v1",
            "requested_features": sorted(features),
            "targets": [],
            "error": {"code": code, "message": message},
        }

    def _task_context_span(self, node: dict, evidence_id: str, commit: str) -> dict | None:
        path = self._source_path(node)
        repository = decode_row(self.storage.repository()) if self.storage.repository() else {}
        root = Path(repository.get("root_path") or "")
        if not path or not root:
            return None
        try:
            lines = (root / path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return None
        start = max(int(node.get("start_line") or 1), 1)
        end = min(max(int(node.get("end_line") or start), start), len(lines))
        # Keep the target line in a bounded, complete source range.
        end = min(end, start + 19)
        before = "\n".join(lines[:start - 1])
        text = "\n".join(lines[start - 1:end])
        start_byte = len((before + ("\n" if before else "")).encode("utf-8"))
        end_byte = start_byte + len(text.encode("utf-8"))
        return {"fact_id": f"span:{commit}:{path}:{start_byte}:{end_byte}",
                "kind": "span", "file": path, "start_line": start, "end_line": end,
                "start_byte": start_byte, "end_byte": end_byte, "content": text,
                "evidence_ids": [evidence_id]}

    @staticmethod
    def _task_context_trim(value: dict) -> dict:
        """Apply the frozen 6,000-byte response cap without partial facts."""
        omitted = {"primary_targets": 0, "relations": 0, "candidates": 0, "source_spans": 0}
        def encoded() -> int:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        # Lowest-ranked items are at the tail of these already deterministic lists.
        for name in ("source_spans", "candidates", "relations", "primary_targets"):
            minimum = 1 if name == "primary_targets" else 0
            while encoded() > 6000 and len(value[name]) > minimum:
                value[name].pop()
                omitted[name] += 1
        value["truncation"] = {"truncated": any(omitted.values()), "omitted": omitted,
                               "max_serialized_chars": 6000}
        return value

    def task_context(self, symbol: str) -> dict:
        """Frozen D9-C2 V0 compact context view for one explicit code symbol."""
        definition = self.get_definition(symbol)
        anchor = definition["data"]
        if not anchor:
            return {"version": "task-context-v0", "status": "not_found", "anchor": symbol,
                    "primary_targets": [], "relations": [], "candidates": [], "source_spans": [],
                    "expansion_handles": [], "truncation": {"truncated": False, "omitted": {},
                    "max_serialized_chars": 6000}}
        repository, commit = self._context()
        anchor_evidence = self._node_evidence(anchor)
        callers, callees, references = (self.get_callers(anchor["id"]),
                                        self.get_callees(anchor["id"]),
                                        self.get_references(anchor["id"]))
        relation_evidence = [*callers["evidence"], *callees["evidence"], *references["evidence"]][:12]
        relations = [{"fact_id": f"relation:{item['relation']}:{item['source_id']}:{item['target_id']}",
                      "kind": "relation", "relation": item["relation"],
                      "source_symbol_id": item["source_id"], "target_symbol_id": item["target_id"],
                      "evidence_ids": [item["evidence_id"]]} for item in relation_evidence]
        related = [*callers["data"], *callees["data"], *references["data"]]
        primary = [{"fact_id": f"symbol:{anchor['id']}", "kind": "symbol",
                    "symbol_id": anchor["id"], "qualified_name": anchor.get("qualified_name"),
                    "file": self._source_path(anchor), "start_line": anchor.get("start_line"),
                    "end_line": anchor.get("end_line"), "evidence_ids": [anchor_evidence.evidence_id]}]
        if related:
            node = related[0]
            primary.append({"fact_id": f"symbol:{node['id']}", "kind": "symbol", "symbol_id": node["id"],
                            "qualified_name": node.get("qualified_name"), "file": self._source_path(node),
                            "start_line": node.get("start_line"), "end_line": node.get("end_line"),
                            "evidence_ids": [self._node_evidence(node).evidence_id]})
        raw = self.get_raw_references(owner_symbol_id=anchor["id"])["data"]
        candidates = []
        for row in raw:
            if row.get("status") == "resolved":
                continue
            reference_id = row["id"]
            symbols = row.get("candidate_symbols") or []
            if symbols:
                for candidate in symbols:
                    candidates.append({"fact_id": f"candidate:{reference_id}:{candidate}", "kind": "candidate",
                                       "reference_id": reference_id, "symbol_id": candidate,
                                       "status": row.get("status"), "evidence_ids": [self._raw_reference_evidence(row).evidence_id]})
            else:
                candidates.append({"fact_id": f"unresolved:{reference_id}", "kind": "unresolved",
                                   "reference_id": reference_id, "status": row.get("status"),
                                   "evidence_ids": [self._raw_reference_evidence(row).evidence_id]})
        candidates = candidates[:6]
        spans = [self._task_context_span(anchor, anchor_evidence.evidence_id, commit)]
        spans.extend(self._task_context_span(node, self._node_evidence(node).evidence_id, commit)
                     for node in related[:3])
        value = {"version": "task-context-v0", "status": "available", "anchor": symbol,
                 "repository": repository, "base_commit": commit, "primary_targets": primary[:2],
                 "relations": relations, "candidates": candidates, "source_spans": [item for item in spans if item],
                 "expansion_handles": [{"symbol_id": item["symbol_id"], "command": "explain-symbol"}
                                       for item in primary], "truncation": {"truncated": False, "omitted": {},
                                       "max_serialized_chars": 6000}}
        return self._task_context_trim(value)

    def explain_symbol(self, symbol: str, *, max_evidence: int = 20, max_symbols: int = 8,
                       max_edges: int = 12, max_sections: int = 6,
                       max_primary: int | None = None, max_supporting: int | None = None,
                       max_total: int | None = None, bundle_profile: str = "generic",
                       focus_terms: tuple[str, ...] = (), analysis=None) -> dict:
        started = time.perf_counter()
        budget = self._budget(max_evidence, max_symbols, max_edges, max_sections,
                              max_primary, max_supporting, max_total)
        definition = self.get_definition(symbol)
        anchor = definition["data"]
        if not anchor:
            bundle = EvidenceBundle.create(
                anchor=symbol, intent="explain_symbol", summary=f"Symbol not found: {symbol}",
                primary_evidence=[], supporting_evidence=[], related_entities=[],
                uncertainties=["unresolved symbol anchor"], generation=self.graph_generation,
                budget=budget, profile=bundle_profile, focus_terms=focus_terms,
            )
            result = self._bundle_result(bundle, started)
            features = _parse_analysis_request(analysis)
            if features:
                result["analysis"] = {
                    feature: {"status": "not_available", "reason": "symbol_not_found",
                              "coverage": {"type": "none"}}
                    for feature in features
                }
            return result
        callers, callees = self.get_callers(anchor["id"]), self.get_callees(anchor["id"])
        references = self.get_references(anchor["id"])
        documents = self._cross_layer_neighbors(anchor["id"], CROSS_RELATIONS, True)
        relation_evidence = (self._evidence_from(callers) + self._evidence_from(callees)
                             + self._evidence_from(references))[:budget.max_edges]
        document_nodes = documents["data"][:budget.max_sections]
        document_node_evidence = list({
            item.evidence_id: item for item in (
                self._node_evidence(document) for document in document_nodes
            )
        }.values())
        document_link_evidence = self._evidence_from(documents)[:budget.max_sections]
        shard_evidence: list[Evidence] = []
        if anchor.get("shard_id"):
            repository, commit = self._context()
            shard = self._shard(anchor["shard_id"])
            shard_evidence.append(Evidence.create(
                kind="SHARD_RELATION", source_id=anchor["id"], relation="MEMBER_OF",
                target_id=anchor["shard_id"], repository=repository, commit=commit,
                generation=self.graph_generation, provenance="directory_shard",
                confidence=1.0, source_path=self._source_path(anchor),
                start_line=anchor.get("start_line"), end_line=anchor.get("end_line"),
                summary=f"{anchor['qualified_name']} belongs to {(shard or {}).get('path', anchor['shard_id'])}",
                fact_id=f"{anchor['id']}:{anchor['shard_id']}",
                metadata={"direct": True, "boundary_relevant": False},
            ))
        related = [anchor, *callers["data"], *callees["data"], *documents["data"]]
        bundle = EvidenceBundle.create(
            anchor=symbol, intent="explain_symbol",
            summary=(f"{anchor['kind']} {anchor['qualified_name']}"
                     + (f" {anchor['signature']}" if anchor.get("signature") else "")),
            primary_evidence=[*self._evidence_from(definition), *document_node_evidence],
            supporting_evidence=[*relation_evidence, *shard_evidence, *document_link_evidence],
            related_entities=related, uncertainties=[], generation=self.graph_generation,
            budget=budget, profile=bundle_profile, focus_terms=focus_terms,
        )
        result = self._bundle_result(bundle, started)
        features = _parse_analysis_request(analysis)
        if features:
            result["analysis"] = self._analysis_sections(anchor["id"], features)
        return result

    def _resolve_document(self, document: str) -> dict | None:
        value = document.casefold()
        candidates = []
        for raw in self.view.all("Node"):
            row = decode_row(raw)
            if row.get("kind") not in {"Document", "DocumentSection", "Requirement",
                                       "Spec", "ShardDocument"}:
                continue
            metadata = row.get("metadata") or {}
            fields = [row.get("id", ""), row.get("qualified_name", ""), row.get("name", ""),
                      metadata.get("path", ""), metadata.get("heading_path", "")]
            exact = any(str(field).casefold() == value for field in fields)
            contains = any(value in str(field).casefold() for field in fields if field)
            if exact or contains:
                candidates.append((0 if exact else 1, row.get("qualified_name", ""), row["id"], row))
        return min(candidates)[-1] if candidates else None

    def find_related_code(self, document: str, *, max_evidence: int = 20,
                          max_symbols: int = 8, max_edges: int = 12,
                          max_sections: int = 6, bundle_profile: str = "generic",
                          focus_terms: tuple[str, ...] = (), max_primary: int | None = None,
                          max_supporting: int | None = None, max_total: int | None = None,
                          analysis=None, analysis_symbol_id: str | None = None) -> dict:
        started = time.perf_counter()
        budget = self._budget(max_evidence, max_symbols, max_edges, max_sections,
                              max_primary, max_supporting, max_total)
        source = self._resolve_document(document)
        if not source:
            bundle = EvidenceBundle.create(
                anchor=document, intent="find_related_code", summary=f"Document not found: {document}",
                primary_evidence=[], supporting_evidence=[], related_entities=[],
                uncertainties=["unresolved document or section anchor"],
                generation=self.graph_generation, budget=budget,
                profile=bundle_profile, focus_terms=focus_terms,
            )
            return self._bundle_result(bundle, started)
        relations = sorted(CROSS_RELATIONS)
        placeholders = ",".join("?" for _ in relations)
        edges = self.view.query(
            "Edge", f"SELECT * FROM edges WHERE src_id=? AND type IN ({placeholders})",
            (source["id"], *relations),
            lambda row: row.get("src_id") == source["id"] and row.get("type") in CROSS_RELATIONS,
        )
        target_rows = self.view.by_ids("Node", [edge["dst_id"] for edge in edges])
        targets = {row["id"]: decode_row(row) for row in target_rows}
        node_map = {source["id"]: source, **targets}
        resolved = [self._edge_evidence(decode_row(edge), node_map) for edge in edges]
        raw_rows = self.view.query(
            "RawEvidenceLink", "SELECT * FROM raw_evidence_links WHERE source_node_id=?",
            (source["id"],), lambda row: row.get("source_node_id") == source["id"],
        )
        raw = [self._raw_link_evidence(decode_row(row)) for row in raw_rows]
        candidates = [item for item in raw if item.metadata.get("resolution_status") != "resolved"]
        primary = [self._node_evidence(source)]
        primary += resolved[:budget.max_edges]
        related = sorted(targets.values(), key=lambda row: (row.get("qualified_name", ""), row["id"]))
        shards = sorted({item.get("shard_id") for item in related if item.get("shard_id")})
        uncertainties = [f"{len(candidates)} ambiguous or unresolved candidate links"] if candidates else []
        if not resolved:
            uncertainties.append("no resolved code evidence")
        bundle = EvidenceBundle.create(
            anchor=document, intent="find_related_code",
            summary=f"Resolved code for {source.get('qualified_name')}: {len(resolved)} links; shards={shards}",
            primary_evidence=primary, supporting_evidence=candidates,
            related_entities=related, uncertainties=uncertainties,
            generation=self.graph_generation, budget=budget,
            profile=bundle_profile, focus_terms=focus_terms,
        )
        result = self._bundle_result(bundle, started)
        features = _parse_analysis_request(analysis)
        if not features:
            return result
        selectable = {
            item["id"]: item for item in result["bundle"]["related_entities"]
            if item.get("kind") in SYMBOL_KINDS
        }
        if not selectable:
            result["analysis"] = self._analysis_v1_error(
                features, "ANALYSIS_NO_SELECTABLE_SYMBOL",
                "analysis requires a code symbol returned by this evidence bundle",
            )
        elif analysis_symbol_id is None:
            result["analysis"] = self._analysis_v1_error(
                features, "ANALYSIS_SYMBOL_ID_REQUIRED",
                "analysis-symbol-id must select one returned code symbol",
            )
        elif analysis_symbol_id not in selectable:
            result["analysis"] = self._analysis_v1_error(
                features, "ANALYSIS_SYMBOL_ID_NOT_RETURNED",
                "analysis-symbol-id is not a code symbol returned by this evidence bundle",
            )
        else:
            result["analysis"] = self._analysis_v1(selectable[analysis_symbol_id], features)
        return result

    def find_related_documents(self, symbol: str, *, max_evidence: int = 20,
                               max_symbols: int = 8, max_edges: int = 12,
                               max_sections: int = 6, bundle_profile: str = "generic",
                               focus_terms: tuple[str, ...] = (), max_primary: int | None = None,
                               max_supporting: int | None = None, max_total: int | None = None,
                               analysis=None) -> dict:
        started = time.perf_counter()
        budget = self._budget(max_evidence, max_symbols, max_edges, max_sections,
                              max_primary, max_supporting, max_total)
        anchor = self._resolve(symbol)
        if not anchor:
            bundle = EvidenceBundle.create(
                anchor=symbol, intent="find_related_documents", summary=f"Symbol not found: {symbol}",
                primary_evidence=[], supporting_evidence=[], related_entities=[],
                uncertainties=["unresolved symbol anchor"], generation=self.graph_generation,
                budget=budget, profile=bundle_profile, focus_terms=focus_terms,
            )
            return self._bundle_result(bundle, started)
        anchor = decode_row(anchor)
        relations = sorted(CROSS_RELATIONS)
        placeholders = ",".join("?" for _ in relations)
        edges = self.view.query(
            "Edge", f"SELECT * FROM edges WHERE dst_id=? AND type IN ({placeholders})",
            (anchor["id"], *relations),
            lambda row: row.get("dst_id") == anchor["id"] and row.get("type") in CROSS_RELATIONS,
        )
        sources = {row["id"]: decode_row(row) for row in self.view.by_ids(
            "Node", [edge["src_id"] for edge in edges])}
        node_map = {anchor["id"]: anchor, **sources}
        link_evidence = [self._edge_evidence(decode_row(edge), node_map) for edge in edges]
        documents = sorted(sources.values(),
                           key=lambda row: (row.get("qualified_name", ""), row["id"]))[:budget.max_sections]
        primary = [self._node_evidence(item) for item in documents]
        uncertainties = [] if documents else ["no resolved document evidence"]
        bundle = EvidenceBundle.create(
            anchor=symbol, intent="find_related_documents",
            summary=f"Related document sections for {anchor['qualified_name']}: {len(documents)}",
            primary_evidence=primary, supporting_evidence=link_evidence[:budget.max_edges],
            related_entities=documents, uncertainties=uncertainties,
            generation=self.graph_generation, budget=budget,
            profile=bundle_profile, focus_terms=focus_terms,
        )
        result = self._bundle_result(bundle, started)
        features = _parse_analysis_request(analysis)
        if features:
            result["analysis"] = self._analysis_v1(anchor, features)
        return result

    def explain_module(self, module: str, *, max_evidence: int = 20,
                       max_symbols: int = 8, max_edges: int = 12,
                       max_sections: int = 6, bundle_profile: str = "generic",
                       focus_terms: tuple[str, ...] = (), max_primary: int | None = None,
                       max_supporting: int | None = None, max_total: int | None = None) -> dict:
        started = time.perf_counter()
        budget = self._budget(max_evidence, max_symbols, max_edges, max_sections,
                              max_primary, max_supporting, max_total)
        shard = self._shard(module)
        if not shard:
            node = self._resolve(module)
            shard = self._shard(node["shard_id"]) if node and node.get("shard_id") else None
        if not shard:
            bundle = EvidenceBundle.create(
                anchor=module, intent="explain_module", summary=f"Module not found: {module}",
                primary_evidence=[], supporting_evidence=[], related_entities=[],
                uncertainties=["unresolved shard or symbol anchor"], generation=self.graph_generation,
                budget=budget, profile=bundle_profile, focus_terms=focus_terms,
            )
            return self._bundle_result(bundle, started)
        rows = self.view.query(
            "Node", "SELECT * FROM nodes WHERE shard_id=?", (shard["shard_id"],),
            lambda row: row.get("shard_id") == shard["shard_id"],
        )
        nodes = [decode_row(row) for row in rows if row.get("kind") in SYMBOL_KINDS]
        public = set(shard.get("public_symbols") or [])
        nodes.sort(key=lambda row: (0 if row["id"] in public else 1,
                                   row.get("qualified_name", ""), row["id"]))
        primary_nodes = nodes[:budget.max_symbols]
        dependencies = self.get_dependencies(shard["shard_id"])
        dependents = self.get_dependents(shard["shard_id"])
        boundary = (self._evidence_from(dependencies) + self._evidence_from(dependents))[:budget.max_edges]
        node_ids = {node["id"] for node in nodes}
        documents: list[dict] = []
        document_links: list[Evidence] = []
        if node_ids:
            node_placeholders = ",".join("?" for _ in node_ids)
            relations = sorted(CROSS_RELATIONS)
            relation_placeholders = ",".join("?" for _ in relations)
            edges = self.view.query(
                "Edge", f"SELECT * FROM edges WHERE dst_id IN ({node_placeholders}) "
                f"AND type IN ({relation_placeholders})",
                (*sorted(node_ids), *relations),
                lambda row: row.get("dst_id") in node_ids and row.get("type") in CROSS_RELATIONS,
            )
            source_rows = self.view.by_ids("Node", [edge["src_id"] for edge in edges])
            sources = {row["id"]: decode_row(row) for row in source_rows}
            node_map = {node["id"]: node for node in nodes} | sources
            document_links = [self._edge_evidence(decode_row(edge), node_map) for edge in edges]
            documents = sorted(sources.values(), key=lambda row: (row["qualified_name"], row["id"]))[:budget.max_sections]
        bundle = EvidenceBundle.create(
            anchor=module, intent="explain_module",
            summary=(f"Shard {shard['path']}: {len(nodes)} symbols, "
                     f"{len(dependencies['data'])} outbound and {len(dependents['data'])} inbound dependencies"),
            primary_evidence=[self._node_evidence(node) for node in primary_nodes],
            supporting_evidence=[*boundary, *document_links[:budget.max_sections]],
            related_entities=[*primary_nodes, *dependencies["data"], *dependents["data"], *documents],
            uncertainties=[], generation=self.graph_generation, budget=budget,
            profile=bundle_profile, focus_terms=focus_terms,
        )
        return self._bundle_result(bundle, started)

    def trace_evidence(self, requested_id: str, *, max_evidence: int = 1,
                       max_symbols: int = 4, max_edges: int = 4,
                       max_sections: int = 2) -> dict:
        started = time.perf_counter()
        budget = self._budget(max_evidence, max_symbols, max_edges, max_sections)
        found: Evidence | None = None
        related: list[dict] = []
        repository, commit = self._context()
        if requested_id.startswith(("E-CODE-", "E-DOC-")):
            for raw in self.view.all("Node"):
                node = decode_row(raw)
                is_document = node.get("kind") in {
                    "Document", "DocumentSection", "Requirement", "Spec", "ShardDocument",
                }
                kind = "DOCUMENT_SECTION" if is_document else "CODE_DEFINITION"
                relation = "DOCUMENTS" if is_document else "DEFINES"
                candidate = evidence_id(
                    kind, repository, str(node.get("file_id") or node["id"]), relation,
                    node["id"], node["id"],
                )
                if candidate == requested_id:
                    item = self._node_evidence(node)
                    found, related = item, [node]; break
        elif requested_id.startswith(("E-CALL-", "E-DEP-", "E-XLINK-")):
            for raw in self.view.all("Edge"):
                edge = decode_row(raw)
                relation = edge["type"]
                kind = ("CALL_RELATION" if relation == "CALLS" else
                        "REFERENCE" if relation == "REFERENCES" else
                        "CROSS_LAYER_LINK" if relation in CROSS_RELATIONS else "DEPENDENCY")
                candidate = evidence_id(
                    kind, repository, edge["src_id"], relation, edge["dst_id"], edge["id"],
                )
                if candidate == requested_id:
                    nodes = {row["id"]: decode_row(row) for row in self.view.by_ids(
                        "Node", (edge["src_id"], edge["dst_id"]))}
                    item = self._edge_evidence(edge, nodes)
                    found = item; related = [nodes.get(edge["src_id"], {}), nodes.get(edge["dst_id"], {})]; break
            if found is None and requested_id.startswith("E-XLINK-"):
                for raw in self.view.all("RawEvidenceLink"):
                    row = decode_row(raw)
                    targets = row.get("candidate_targets") or []
                    target = (row.get("resolved_target_id") or next(
                        iter(targets), f"raw:{row.get('raw_anchor', '')}"))
                    relation = ("CANDIDATE" if row.get("resolution_status") != "resolved"
                                else "DOCUMENTS")
                    candidate = evidence_id(
                        "CROSS_LAYER_LINK", repository, row["source_node_id"], relation,
                        target, row["id"],
                    )
                    if candidate == requested_id:
                        item = self._raw_link_evidence(row)
                        found, related = item, [row]; break
        elif requested_id.startswith("E-REF-"):
            for raw in self.view.all("RawReference"):
                row = decode_row(raw)
                target = (row.get("resolved_symbol_id") or next(
                    iter(row.get("candidate_symbols") or []), f"raw:{row.get('raw_name', '')}"))
                relation = row.get("reference_type") or "REFERENCES"
                candidate = evidence_id(
                    "REFERENCE", repository, row["owner_symbol_id"], relation, target, row["id"],
                )
                if candidate == requested_id:
                    item = self._raw_reference_evidence(row)
                    found, related = item, [row]; break
        elif requested_id.startswith("E-SHARD-"):
            shards = {row["shard_id"]: decode_row(row) for row in self.view.all("Shard")}
            for edge in self.view.all("BoundaryEdge"):
                candidate = evidence_id(
                    "SHARD_RELATION", repository, edge["src_shard_id"], edge["edge_type"],
                    edge["dst_shard_id"], edge["edge_id"],
                )
                if candidate == requested_id:
                    item = self._shard_edge_evidence(
                        edge, (shards.get(edge["src_shard_id"]) or {}).get("path"))
                    found = item
                    related = [shards.get(edge["src_shard_id"], {}),
                               shards.get(edge["dst_shard_id"], {})]
                    break
            if found is None:
                for raw in self.view.all("Node"):
                    node = decode_row(raw)
                    if not node.get("shard_id"):
                        continue
                    shard = shards.get(node["shard_id"])
                    fact_id = f"{node['id']}:{node['shard_id']}"
                    candidate = evidence_id(
                        "SHARD_RELATION", repository, node["id"], "MEMBER_OF",
                        node["shard_id"], fact_id,
                    )
                    if candidate != requested_id:
                        continue
                    item = Evidence.create(
                        kind="SHARD_RELATION", source_id=node["id"], relation="MEMBER_OF",
                        target_id=node["shard_id"], repository=repository, commit=commit,
                        generation=self.graph_generation, provenance="directory_shard",
                        confidence=1.0, source_path=self._source_path(node),
                        start_line=node.get("start_line"), end_line=node.get("end_line"),
                        summary=f"{node['qualified_name']} belongs to {(shard or {}).get('path', node['shard_id'])}",
                        fact_id=fact_id,
                        metadata={"direct": True, "boundary_relevant": False},
                    )
                    found, related = item, [node, shard or {}]
                    break
        bundle = EvidenceBundle.create(
            anchor=requested_id, intent="trace_evidence",
            summary=(f"Evidence trace for {requested_id}" if found else f"Evidence not found: {requested_id}"),
            primary_evidence=[found] if found else [], supporting_evidence=[],
            related_entities=related, uncertainties=[] if found else ["unknown evidence ID"],
            generation=self.graph_generation, budget=budget,
        )
        return self._bundle_result(bundle, started)

    def status(self) -> dict:
        started = time.perf_counter()
        repository = self.storage.repository()
        counts = {"files": self.storage.row("SELECT COUNT(*) AS count FROM files")["count"],
                  "shards": len(self.view.all("Shard")), "nodes": len(self.view.all("Node")),
                  "edges": len(self.view.all("Edge")),
                  "raw_references": len(self.view.all("RawReference")),
                  "raw_evidence_links": len(self.view.all("RawEvidenceLink"))}
        latest = self.storage.row("SELECT * FROM graph_generation ORDER BY generation DESC LIMIT 1")
        return self._result({"repository": decode_row(repository) if repository else None,
                             "counts": counts,
                             "latest_generation": decode_row(latest) if latest else None,
                             "database_size": self.storage.database_size,
                             "overlay_size": sum(item.database_size for item in self.view.layers)}, started)
