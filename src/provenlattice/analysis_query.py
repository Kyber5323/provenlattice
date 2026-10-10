"""Read-only query surface over the derived-analysis store.

Query never creates, rebuilds or refreshes analysis data: the freshness gate
follows the D1 contract (state row vs base file_state hash + registry
version) and stale/missing requests fail closed with empty payloads.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from .analysis import ANALYZER_VERSIONS
from .orchestrator import AnalysisRequestError
from .storage import SQLiteStorage

SUPPORTED_QUERY_FEATURES = frozenset({"ast", "cfg"})
QUERY_ANALYZERS = {
    ("ast", "python"): "ast:python",
    ("cfg", "python"): "cfg:python",
    ("ast", "c"): "ast:c",
}
FUNCTION_KINDS = frozenset({
    "function_definition", "async_function_definition", "FUNCTION",
})
ANALYSIS_RESPONSE_NODE_LIMIT = 400


def _selector(file_path, symbol_id) -> dict:
    if file_path is not None:
        return {"file_path": file_path}
    return {"symbol_id": symbol_id}


def _envelope(feature, analyzer, selector, status, reason, coverage) -> dict:
    return {
        "feature": feature,
        "analyzer": analyzer,
        "selector": selector,
        "status": status,
        "reason": reason,
        "coverage": coverage,
        "nodes": [],
        "edges": [],
    }


def _coverage(state) -> dict:
    if state is None:
        return {"type": "none"}
    scope = state.get("metadata", {}).get("scope")
    if scope:
        coverage = {"type": "scoped", "scope": scope}
        metadata = state.get("metadata", {})
        for key in (
            "analysis_status", "partial", "parse_coverage",
            "error_node_count", "missing_node_count",
        ):
            if key in metadata:
                coverage[key] = metadata[key]
        return coverage
    return {"type": "derived_only"}


def _stale_reasons(storage, file_id: str, state) -> list[str]:
    reasons: list[str] = []
    if state["status"] != "current":
        reasons.append("status_not_current")
    base = storage.row(
        "SELECT source_hash FROM file_state WHERE file_id=?", (file_id,)
    )
    if base is None:
        reasons.append("file_missing_from_base")
    elif base["source_hash"] != state["source_hash"]:
        reasons.append("source_hash_changed")
    return reasons


def _read_state(storage, file_id: str, analyzer: str) -> dict:
    table = storage.row(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_state'"
    )
    if table is None:
        return {"status": "not_built", "reason": "analysis_not_built", "state": None}
    state = storage.row(
        "SELECT * FROM analysis_state WHERE file_id=? AND feature=?",
        (file_id, analyzer),
    )
    if state is None:
        return {"status": "not_built", "reason": "analysis_not_built", "state": None}
    state = dict(state)
    state["metadata"] = json.loads(state["metadata"])
    reasons = _stale_reasons(storage, file_id, state)
    if state["feature_version"] != ANALYZER_VERSIONS[analyzer]:
        reasons.append("feature_version_changed")
    if reasons:
        return {"status": "stale", "reason": "+".join(reasons), "state": state}
    nodes = [
        dict(row) for row in storage.rows(
            "SELECT * FROM analysis_nodes WHERE file_id=? AND feature=? "
            "ORDER BY analysis_node_id", (file_id, analyzer))
    ]
    edges = [
        dict(row) for row in storage.rows(
            "SELECT * FROM analysis_edges WHERE file_id=? AND feature=? "
            "ORDER BY analysis_edge_id", (file_id, analyzer))
    ]
    for row in nodes:
        row["payload"] = json.loads(row["payload"])
    for row in edges:
        row["payload"] = json.loads(row["payload"])
    return {"status": "available", "reason": None, "state": state,
            "nodes": nodes, "edges": edges}


def _path_index(node) -> int:
    path = node["payload"].get("structural_path") or ""
    tail = path.rsplit("[", 1)[-1].rstrip("]")
    try:
        return int(tail)
    except ValueError:
        return 0


def _check_ast_integrity(nodes) -> str | None:
    ids = {node["analysis_node_id"] for node in nodes}
    roots = [node for node in nodes if node["payload"].get("parent_id") is None]
    if len(roots) != 1:
        return "root_missing" if not roots else "multiple_roots"
    for node in nodes:
        parent = node["payload"].get("parent_id")
        if parent is not None and parent not in ids:
            return "parent_missing"
    return None


def _check_cfg_integrity(nodes, edges) -> str | None:
    ids = {node["analysis_node_id"] for node in nodes}
    owners = {node["payload"].get("owner_symbol_id") for node in nodes}
    if None in owners or not owners:
        return "node_missing_owner"
    for owner in owners:
        group = [node for node in nodes
                 if node["payload"].get("owner_symbol_id") == owner]
        if sum(1 for node in group if node["kind"] == "entry") != 1:
            return "entry_missing"
        if sum(1 for node in group if node["kind"] == "exit") != 1:
            return "exit_missing"
    for edge in edges:
        if edge["src_id"] not in ids or edge["dst_id"] not in ids:
            return "edge_endpoint_missing"
        src = next(node for node in nodes if node["analysis_node_id"] == edge["src_id"])
        if src["payload"].get("owner_symbol_id") != edge["payload"].get("owner_symbol_id"):
            return "edge_owner_mismatch"
    return None


def _build_ast_tree(nodes, root_id: str) -> dict:
    children_map: dict[str | None, list[dict]] = {}
    for node in nodes:
        children_map.setdefault(node["payload"].get("parent_id"), []).append(node)

    def emit(node) -> dict:
        payload = node["payload"]
        emitted = {
            "id": node["analysis_node_id"],
            "kind": node["kind"],
            "structural_path": payload.get("structural_path"),
            "start_byte": payload.get("start_byte"),
            "end_byte": payload.get("end_byte"),
            "children": [],
        }
        for key in ("language", "parser_kind", "name", "child_order", "partial"):
            if key in payload:
                emitted[key] = payload[key]
        children = sorted(
            children_map.get(node["analysis_node_id"], []),
            key=lambda child: (_path_index(child),
                               child["payload"].get("start_byte") or 0),
        )
        for child in children:
            emitted["children"].append(emit(child))
        return emitted

    root_row = next(node for node in nodes if node["analysis_node_id"] == root_id)
    return emit(root_row)


def query_analysis(repo: str | Path, feature: str, *, file_path=None,
                   symbol_id=None, database: str | Path | None = None) -> dict:
    root = Path(repo).resolve()
    feature_key = str(feature).strip().lower()
    if feature_key not in SUPPORTED_QUERY_FEATURES:
        return _envelope(feature, None, _selector(file_path, symbol_id),
                         "unsupported", "unsupported_feature", {"type": "none"})
    selectors = [name for name, value in
                 (("file_path", file_path), ("symbol_id", symbol_id)) if value]
    if len(selectors) != 1:
        raise AnalysisRequestError(
            "invalid_request", "exactly one of file_path or symbol_id is required")
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if repository is None:
            raise AnalysisRequestError(
                "base_graph_required", "run `provenlattice index` before query")
        repo_id = repository["repo_id"]
        if symbol_id is not None:
            return _query_symbol(storage, repo_id, feature_key, symbol_id)
        return _query_file(storage, repo_id, feature_key,
                           PurePosixPath(str(file_path).replace("\\", "/")).as_posix())


def _analysis_language(path: str, stored_language: str | None) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".c", ".h"}:
        return "c"
    return stored_language


def _analyzer_for(feature: str, language: str | None) -> tuple[str | None, str | None]:
    analyzer = QUERY_ANALYZERS.get((feature, language))
    if analyzer is not None:
        return analyzer, None
    if feature == "cfg" and language == "c":
        return "cfg:c", "not_implemented"
    return (f"{feature}:{language}" if language else None), "unsupported_language"


def _query_file(storage, repo_id: str, feature_key: str, relative: str) -> dict:
    selector = {"file_path": relative}
    file_row = storage.row(
        "SELECT file_id, language FROM files WHERE repo_id=? AND path=?",
        (repo_id, relative),
    )
    if file_row is None:
        return _envelope(feature_key, analyzer, selector, "not_built",
                         "file_not_in_base_graph", {"type": "none"})
    analyzer, unsupported = _analyzer_for(
        feature_key, _analysis_language(relative, file_row["language"])
    )
    if unsupported is not None:
        return _envelope(feature_key, analyzer, selector, "unsupported",
                         unsupported, {"type": "none"})
    file_id = file_row["file_id"]
    read = _read_state(storage, file_id, analyzer)
    if read["status"] != "available":
        return _envelope(feature_key, analyzer, selector, read["status"],
                         read["reason"], _coverage(read["state"]))
    nodes, edges = read["nodes"], read["edges"]
    integrity = (_check_cfg_integrity(nodes, edges) if feature_key == "cfg"
                 else _check_ast_integrity(nodes))
    if integrity is not None:
        return _envelope(feature_key, analyzer, selector, "corrupt",
                         integrity, _coverage(read["state"]))
    result = _envelope(feature_key, analyzer, selector, "available", None,
                       _coverage(read["state"]))
    result.update({"file_id": file_id, "nodes": nodes, "edges": edges})
    if feature_key == "ast":
        roots = [node for node in nodes if node["payload"].get("parent_id") is None]
        result["root_id"] = roots[0]["analysis_node_id"] if len(roots) == 1 else None
    return result


def _query_symbol(storage, repo_id: str, feature_key: str, symbol_id: str) -> dict:
    selector = {"symbol_id": symbol_id}
    analyzer = QUERY_ANALYZERS.get((feature_key, "python"))
    symbol = storage.row(
        "SELECT id, kind, name, qualified_name, file_id FROM nodes WHERE id=?",
        (symbol_id,),
    )
    if symbol is None:
        return _envelope(feature_key, analyzer, selector, "not_available",
                         "symbol_not_found", {"type": "none"})
    if symbol["file_id"] is None:
        return _envelope(feature_key, analyzer, selector, "not_available",
                         "analysis_not_available", {"type": "none"})
    file_id = symbol["file_id"]
    file_row = storage.row(
        "SELECT path, language FROM files WHERE file_id=?", (file_id,)
    )
    language = _analysis_language(
        file_row["path"], file_row["language"]
    ) if file_row is not None else None
    analyzer, unsupported = _analyzer_for(feature_key, language)
    if unsupported is not None:
        return _envelope(feature_key, analyzer, selector, "unsupported",
                         unsupported, {"type": "none"})
    read = _read_state(storage, file_id, analyzer)
    if read["status"] == "not_built":
        return _envelope(feature_key, analyzer, selector, "not_built",
                         "analysis_not_built", {"type": "none"})
    if read["status"] == "stale":
        return _envelope(feature_key, analyzer, selector, "stale",
                         read["reason"], _coverage(read["state"]))
    if feature_key == "cfg":
        nodes = [node for node in read["nodes"]
                 if node["payload"].get("owner_symbol_id") == symbol_id]
        edges = [edge for edge in read["edges"]
                 if edge["payload"].get("owner_symbol_id") == symbol_id]
        if not nodes:
            return _envelope(feature_key, analyzer, selector, "not_available",
                             "analysis_not_available", _coverage(read["state"]))
        integrity = _check_cfg_integrity(nodes, edges)
        if integrity is not None:
            return _envelope(feature_key, analyzer, selector, "corrupt",
                             integrity, _coverage(read["state"]))
        entries = [node for node in nodes if node["kind"] == "entry"]
        exits = [node for node in nodes if node["kind"] == "exit"]
        result = _envelope(feature_key, analyzer, selector, "available", None,
                           _coverage(read["state"]))
        result.update({
            "file_id": file_id,
            "symbol": {"id": symbol["id"], "kind": symbol["kind"],
                       "name": symbol["name"], "qualified_name": symbol["qualified_name"]},
            "entry_id": entries[0]["analysis_node_id"],
            "exit_id": exits[0]["analysis_node_id"],
            "nodes": nodes, "edges": edges,
        })
        return result
    owner_nodes = [node for node in read["nodes"]
                   if node["payload"].get("owner_symbol_id") == symbol_id
                   and node["kind"] in FUNCTION_KINDS]
    if not owner_nodes:
        return _envelope(feature_key, analyzer, selector, "not_available",
                         "analysis_not_available", _coverage(read["state"]))
    if len(owner_nodes) > 1:
        return _envelope(feature_key, analyzer, selector, "corrupt",
                         "ambiguous_owner_nodes", _coverage(read["state"]))
    function_node = owner_nodes[0]
    prefix = function_node["payload"]["structural_path"] + "/"
    subtree = [function_node] + [
        node for node in read["nodes"]
        if node["payload"].get("structural_path", "").startswith(prefix)
    ]
    ids = {node["analysis_node_id"] for node in subtree}
    for node in subtree:
        if node["analysis_node_id"] == function_node["analysis_node_id"]:
            continue
        parent = node["payload"].get("parent_id")
        if parent is not None and parent not in ids:
            return _envelope(feature_key, analyzer, selector, "corrupt",
                             "parent_missing", _coverage(read["state"]))
    result = _envelope(feature_key, analyzer, selector, "available", None,
                       _coverage(read["state"]))
    result.update({
        "file_id": file_id,
        "symbol": {"id": symbol["id"], "kind": symbol["kind"],
                   "name": symbol["name"], "qualified_name": symbol["qualified_name"]},
        "root": _build_ast_tree(subtree, function_node["analysis_node_id"]),
    })
    return result
