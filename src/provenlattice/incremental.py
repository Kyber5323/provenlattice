from __future__ import annotations

import json
import time
from pathlib import Path

from .graph import build_graph, parsed_from_dict
from .identity import path_id, repository_id, symbol_id
from .impact import compute_impact_frontier
from .models import Delta, Metrics, ParsedFile
from .parser import parse_file_with_diagnostics
from .scanner import scan_repository
from .shard import ShardStrategy
from .storage import SQLiteStorage


def incremental_update(
    repo: str | Path, database: str | Path | None = None, *, strategy: ShardStrategy | None = None,
    repository_id_override: str | None = None,
) -> dict:
    started = time.perf_counter()
    root = Path(repo).resolve()
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    repo_id = repository_id_override or repository_id(root)
    sources = scan_repository(root)
    source_by_path = {source.relative_path: source for source in sources}
    with SQLiteStorage(database) as storage:
        generation = storage.current_generation(repo_id)
        if generation == 0:
            raise RuntimeError("repository is not indexed; run `provenlattice index` first")
        old_states = storage.file_states(repo_id)

        current_paths = set(source_by_path)
        old_paths = set(old_states)
        added = current_paths - old_paths
        deleted = old_paths - current_paths
        modified = {
            path for path in current_paths & old_paths
            if source_by_path[path].source_hash != old_states[path]["source_hash"]
        }
        changed = added | modified | deleted
        if not changed:
            return {
                "repo_id": repo_id,
                "graph_generation": generation,
                "changed": False,
                "delta": Delta().to_dict(),
                "metrics": Metrics(
                    files_scanned=len(sources), files_reused=len(sources),
                    shards_reused=len(storage.shard_fingerprints()), database_size=storage.database_size,
                ).to_dict(),
            }

        # Only past the point where the update is known to touch something: these
        # loads dominate update memory (raw_references alone is ~57% of the
        # database, and nodes.metadata.parsed is 94.8% of the nodes table) and a
        # no-op update must not pay for them. Even a real update now reads only
        # the columns it consumes (appendix A, 0.3): the fat `parsed` blobs
        # appear once in parser_cache and never again.
        old_cache = storage.parser_cache(repo_id)
        old_nodes = storage.snapshot_columns(
            "nodes", ("id", "kind", "source_hash", "start_line", "end_line")
        )
        old_edge_ids = storage.snapshot_columns("edges", ("id",))
        old_boundary = storage.boundary_edges()
        old_nodes_metadata = storage.nodes_metadata()
        old_fingerprints = storage.shard_fingerprints()
        old_semantic_fingerprints = storage.shard_semantic_fingerprints()

        parsed_files = {}
        parse_failures = 0
        syntax_error_files = 0
        for path, source in source_by_path.items():
            if path in added or path in modified:
                try:
                    parsed_files[path], has_error = parse_file_with_diagnostics(
                        source.path, path, source.language
                    )
                except Exception as error:
                    parsed_files[path] = ParsedFile(
                        [], [], [], parser=f"failed:{type(error).__name__}"
                    )
                    has_error = False
                    parse_failures += 1
                syntax_error_files += int(has_error)
            else:
                cached = old_cache.get(path)
                if cached is None:
                    raise RuntimeError(f"missing parser cache for unchanged file: {path}")
                parsed_files[path] = parsed_from_dict(cached)

        changed_file_ids = {path_id(repo_id, "File", path) for path in changed}
        changed_file_rows = storage.nodes_for_files(changed_file_ids)
        old_symbol_facts = {
            row["id"]: (row["name"], row["qualified_name"])
            for row in changed_file_rows
            if row["kind"] in {"File", "Namespace", "Class", "Struct", "Interface", "Function", "Method", "Type"}
        }
        new_symbol_facts: dict[str, tuple[str, str]] = {}
        for path in added | modified:
            file_id = path_id(repo_id, "File", path)
            module_parts = list(Path(path).with_suffix("").parts)
            if module_parts and module_parts[-1] == "__init__":
                module_parts.pop()
            module = ".".join(module_parts)
            new_symbol_facts[file_id] = (Path(path).name, module)
            for draft in parsed_files[path].symbols:
                node_id = symbol_id(
                    repo_id, path, draft.kind, draft.qualified_name, draft.signature
                )
                new_symbol_facts[node_id] = (draft.name, draft.qualified_name)
        changed_symbol_ids = set(old_symbol_facts) ^ set(new_symbol_facts)
        affected_names = {
            value
            for node_id in changed_symbol_ids
            for value in (*old_symbol_facts.get(node_id, ()), *new_symbol_facts.get(node_id, ()))
        }
        affected_reference_ids = storage.references_for_names(affected_names)
        # References the resolver cannot reuse -- those of changed files and
        # those whose candidate names moved -- are re-derived anyway, so they
        # are excluded here rather than loaded and discarded (P1: this table
        # dominates update memory).
        old_references = storage.raw_reference_cache(
            repo_id,
            exclude_file_ids=changed_file_ids,
            exclude_ids=affected_reference_ids,
        )

        new_generation = generation + 1
        repo_id, files, nodes, edges, shards, resolution, new_events, new_edges_semantic = build_graph(
            root, sources, parsed_files, new_generation,
            strategy=strategy,
            old_fingerprints=old_fingerprints,
            old_semantic_fingerprints=old_semantic_fingerprints,
            cached_references=old_references,
            changed_file_ids=changed_file_ids,
            affected_reference_ids=affected_reference_ids,
            repository_id_override=repo_id,
        )
        # Unchanged files were never re-parsed, so they produced no events and
        # the snapshot replace below would delete theirs. Carry them forward
        # under their existing ids: an event id does not depend on the
        # generation, so the rows are still correct as they stand.
        carried_events = [
            event for event in storage.semantic_events(repo_id)
            if event.file_id not in changed_file_ids
        ]
        semantic_events = carried_events + new_events
        # Edges carry forward by the same file_id filter. This is only sound
        # because every edge is intra-method and a method lives in exactly one
        # file (identity is a pure function of the file's path), so a file's
        # edge set changes if and only if that file's events do -- a file that
        # was not re-parsed contributes exactly its old edges and nothing else.
        # `test_every_edge_is_intra_file_and_intra_owner` pins this invariant.
        carried_edges = [
            edge for edge in storage.semantic_edges(repo_id)
            if edge.file_id not in changed_file_ids
        ]
        semantic_edges = carried_edges + new_edges_semantic
        new_nodes = {node.id: node for node in nodes}
        new_edges = {edge.id: edge for edge in edges}
        common_node_ids = set(old_nodes) & set(new_nodes)
        updated_nodes = 0
        for node_id in common_node_ids:
            old = old_nodes[node_id]
            new = new_nodes[node_id]
            # File metadata is the parsed cache: it changes iff the source
            # bytes change, which source_hash already reports, so reading it
            # back here would only re-read 94.8% of the table (B.2.3). Symbol
            # metadata stays compared json-for-json.
            old_meta = "" if old["kind"] == "File" else old_nodes_metadata.get(node_id, "")
            new_meta = "" if old["kind"] == "File" else json.dumps(new.metadata, sort_keys=True)
            if (
                old["source_hash"], old["start_line"], old["end_line"], old_meta
            ) != (
                new.source_hash, new.start_line, new.end_line, new_meta
            ):
                updated_nodes += 1
        new_fingerprints = {shard.shard_id: shard.api_fingerprint for shard in shards}
        dirty = sorted(
            shard.shard_id for shard in shards
            if shard.boundary_dirty
        )
        semantically_dirty = sorted(
            shard.shard_id for shard in shards if shard.semantic_dirty
        )
        file_shards = {item["path"]: item["shard_id"] for item in files}
        updated_shards = sorted(
            {file_shards[path] for path in added | modified if path in file_shards}
            | {
                row["shard_id"] for row in changed_file_rows
                if row["kind"] == "File" and row["shard_id"] is not None
            }
        )
        new_edge_dicts = {edge.id: edge.to_dict() for edge in edges}
        impact = compute_impact_frontier(updated_shards, old_boundary, new_edge_dicts)
        new_boundary_ids = {
            edge_id for edge_id, edge in new_edge_dicts.items()
            if edge.get("metadata", {}).get("scope") == "boundary"
        }
        boundary_edges_reprocessed = len(set(old_boundary) ^ new_boundary_ids)
        delta = Delta(
            nodes_added=len(set(new_nodes) - set(old_nodes)),
            nodes_removed=len(set(old_nodes) - set(new_nodes)),
            nodes_updated=updated_nodes,
            edges_added=len(set(new_edges) - set(old_edge_ids)),
            edges_removed=len(set(old_edge_ids) - set(new_edges)),
            old_api_fingerprint=old_fingerprints,
            new_api_fingerprint=new_fingerprints,
            boundary_dirty=dirty,
            semantic_dirty=semantically_dirty,
            shards_updated=updated_shards,
            references_reprocessed=resolution.reprocessed,
            references_reused=resolution.reused,
            directly_affected_shards=impact["directly_affected_shards"],
            affected_boundary_edges=impact["affected_boundary_edges"],
            impact_frontier_size=impact["impact_frontier_size"],
            wide_impact=impact["wide_impact"],
        )
        status_counts = {status: 0 for status in ("resolved", "ambiguous", "unresolved")}
        for reference in resolution.references:
            status_counts[reference.status] += 1
        metrics = Metrics(
            files_scanned=len(sources), files_parsed=len(added | modified) - parse_failures,
            parse_failures=parse_failures, syntax_error_files=syntax_error_files,
            nodes_created=len(nodes), edges_created=len(edges),
            raw_references=len(resolution.references),
            resolved_references=status_counts["resolved"],
            ambiguous_references=status_counts["ambiguous"],
            unresolved_references=status_counts["unresolved"],
            shards_created=len(shards),
            boundary_edges=sum(edge.metadata.get("scope") == "boundary" for edge in edges),
            files_reparsed=len(added | modified), files_reused=max(len(sources) - len(added | modified), 0),
            references_reprocessed=resolution.reprocessed, references_reused=resolution.reused,
            shards_updated=len(updated_shards), shards_reused=max(len(shards) - len(updated_shards), 0),
            boundary_edges_reprocessed=boundary_edges_reprocessed,
            boundary_edges_reused=max(len(new_boundary_ids) - boundary_edges_reprocessed, 0),
            impact_frontier_size=impact["impact_frontier_size"], wide_impact=impact["wide_impact"],
            semantic_points_created=len(semantic_events),
            semantic_edges_created=len(semantic_edges),
            changed_files=len(changed), affected_nodes=delta.nodes_added + delta.nodes_removed + delta.nodes_updated,
            affected_edges=delta.edges_added + delta.edges_removed,
        )
        storage.replace_snapshot(
            repo_id=repo_id, root=root, generation=new_generation, mode="incremental",
            files=files, nodes=nodes, edges=edges, shards=shards,
            raw_references=resolution.references, metrics=metrics,
            semantic_events=semantic_events, semantic_edges=semantic_edges,
            semantic_fingerprints=[
                {
                    "shard_id": shard.shard_id,
                    "fingerprint": shard.semantic_fingerprint,
                    "metadata": {
                        "api_fingerprint": shard.api_fingerprint,
                        "semantic_dirty": shard.semantic_dirty,
                    },
                }
                for shard in shards
            ],
        )
        metrics.database_size = storage.database_size
        metrics.incremental_time_ms = (time.perf_counter() - started) * 1000
        storage.connection.execute(
            "UPDATE graph_generation SET metrics=? WHERE repo_id=? AND generation=?",
            (json.dumps(metrics.to_dict(), sort_keys=True), repo_id, new_generation),
        )
        storage.connection.commit()
    return {
        "repo_id": repo_id, "graph_generation": new_generation, "changed": True,
        "changes": {"added": sorted(added), "modified": sorted(modified), "deleted": sorted(deleted)},
        "delta": delta.to_dict(), "metrics": metrics.to_dict(), "database": str(database),
    }
