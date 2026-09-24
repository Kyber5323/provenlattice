from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from pathlib import PurePosixPath
from typing import Protocol

from .identity import shard_id
from .models import Edge, Node, Shard


class ShardStrategy(Protocol):
    def path_for(self, relative_path: str) -> str: ...

    def prepare(self, root: Path, sources: list, parsed_files: dict | None = None) -> None: ...


class DirectoryShardStrategy:
    """Group by up to the first two parent directories."""

    name = "directory"

    def __init__(self, levels: int = 2) -> None:
        self.levels = max(1, levels)

    def prepare(self, root: Path, sources: list, parsed_files: dict | None = None) -> None:
        return None

    def path_for(self, relative_path: str) -> str:
        parents = PurePosixPath(relative_path).parts[:-1]
        return "/".join(parents[:self.levels]) if parents else "."


class BuildAwareShardStrategy(DirectoryShardStrategy):
    """Prefer explicit build/package directories, with directory fallback."""

    name = "build-aware"
    BUILD_FILES = {"BUILD", "BUILD.bazel", "CMakeLists.txt", "Makefile", "GNUmakefile", "meson.build"}

    def __init__(self, fallback_levels: int = 2) -> None:
        super().__init__(fallback_levels)
        self._boundaries: list[str] = []

    def prepare(self, root: Path, sources: list, parsed_files: dict | None = None) -> None:
        boundaries = set()
        for path in root.rglob("*"):
            if path.is_file() and path.name in self.BUILD_FILES:
                relative = path.relative_to(root).parent.as_posix()
                if relative != ".":
                    boundaries.add(relative)
        self._boundaries = sorted(boundaries, key=lambda item: (item.count("/"), item), reverse=True)

    def path_for(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        for boundary in self._boundaries:
            if normalized.startswith(boundary + "/"):
                return boundary
        return super().path_for(relative_path)


class StructuralShardStrategy(DirectoryShardStrategy):
    """Split oversized directory groups using deterministic syntax density buckets."""

    name = "structural"

    def __init__(self, max_files: int = 80, fallback_levels: int = 2) -> None:
        super().__init__(fallback_levels)
        self.max_files = max(1, max_files)
        self._assignments: dict[str, str] = {}

    def prepare(self, root: Path, sources: list, parsed_files: dict | None = None) -> None:
        parsed_files = parsed_files or {}
        groups: dict[str, list] = defaultdict(list)
        for source in sources:
            groups[super().path_for(source.relative_path)].append(source)
        for base, members in groups.items():
            if len(members) <= self.max_files:
                continue
            bucket_count = math.ceil(len(members) / self.max_files)
            def density(source) -> int:
                parsed = parsed_files.get(source.relative_path)
                if parsed is None:
                    return 0
                return len(parsed.symbols) + len(parsed.references) + len(parsed.imports)
            ranked = sorted(
                members,
                key=lambda item: (
                    -density(item),
                    item.relative_path,
                ),
            )
            for index, source in enumerate(ranked):
                self._assignments[source.relative_path] = f"{base}/struct-{index % bucket_count}"

    def path_for(self, relative_path: str) -> str:
        return self._assignments.get(relative_path, super().path_for(relative_path))


def fingerprint(nodes: list[Node]) -> tuple[list[str], str]:
    public = sorted(
        f"{node.kind}|{node.qualified_name}|{node.signature or ''}"
        for node in nodes
        if node.kind in {"Namespace", "Class", "Struct", "Interface", "Function", "Method", "Type"}
        and node.metadata.get("public", False)
    )
    digest = hashlib.sha256("\n".join(public).encode("utf-8")).hexdigest()
    return public, digest


def semantic_fingerprint(nodes: list[Node]) -> str:
    """A digest that moves whenever any source in the shard moves.

    Appendix B.2.2, minimal viable form. `fingerprint` above reads only public
    API signatures, so editing a function body leaves it byte-identical. That
    is correct for the code graph -- the topology really did not change -- and
    fatal for the event layer, because every event lives inside a body.

    This is deliberately coarse: it says "something in here changed", not
    "the semantics changed". The appendix's advice is to start here and refine
    once measurement shows the over-read cost, on the grounds that recomputing
    too much is recoverable and missing a change is not.
    """
    sources = sorted(
        f"{node.metadata.get('relative_path', '')}|{node.source_hash or ''}"
        for node in nodes
        if node.kind == "File"
    )
    return hashlib.sha256("\n".join(sources).encode("utf-8")).hexdigest()


def build_shards(
    repo_id: str,
    nodes: list[Node],
    edges: list[Edge],
    generation: int,
    old_fingerprints: dict[str, str] | None = None,
    old_semantic_fingerprints: dict[str, str] | None = None,
) -> list[Shard]:
    grouped: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        if node.shard_id:
            grouped[node.shard_id].append(node)
    edge_counts: dict[str, int] = defaultdict(int)
    node_shards = {node.id: node.shard_id for node in nodes}
    for edge in edges:
        source_shard = node_shards.get(edge.src_id)
        if source_shard:
            edge_counts[source_shard] += 1
        target_shard = node_shards.get(edge.dst_id)
        edge.metadata["scope"] = (
            "boundary" if source_shard and target_shard and source_shard != target_shard else "internal"
        )
        if source_shard:
            edge.metadata["src_shard_id"] = source_shard
        if target_shard:
            edge.metadata["dst_shard_id"] = target_shard

    old_fingerprints = old_fingerprints or {}
    old_semantic_fingerprints = old_semantic_fingerprints or {}
    result: list[Shard] = []
    for sid, shard_nodes in sorted(grouped.items()):
        path = shard_nodes[0].metadata.get("shard_path", ".")
        public, api_hash = fingerprint(shard_nodes)
        semantic_hash = semantic_fingerprint(shard_nodes)
        result.append(
            Shard(
                shard_id=sid,
                repo_id=repo_id,
                path=path,
                nodes_count=len(shard_nodes),
                edges_count=edge_counts[sid],
                public_symbols=public,
                api_fingerprint=api_hash,
                generation=generation,
                boundary_dirty=sid in old_fingerprints and old_fingerprints[sid] != api_hash,
                semantic_fingerprint=semantic_hash,
                semantic_dirty=(
                    sid in old_semantic_fingerprints
                    and old_semantic_fingerprints[sid] != semantic_hash
                ),
            )
        )
    return result


def qualify_shards(shards: list[Shard], nodes: list[Node], edges: list[Edge]) -> dict:
    files = defaultdict(int)
    node_counts = defaultdict(int)
    incident = defaultdict(int)
    for node in nodes:
        if node.shard_id:
            node_counts[node.shard_id] += 1
            files[node.shard_id] += int(node.kind == "File")
    internal = sum(edge.metadata.get("scope") == "internal" for edge in edges)
    boundary = sum(edge.metadata.get("scope") == "boundary" for edge in edges)
    for edge in edges:
        if edge.metadata.get("scope") == "boundary":
            incident[edge.metadata.get("src_shard_id")] += 1
            incident[edge.metadata.get("dst_shard_id")] += 1

    def summary(values: list[int]) -> dict[str, float | int]:
        if not values:
            return {"min": 0, "median": 0, "p95": 0, "max": 0}
        values = sorted(values)
        return {"min": values[0], "median": values[(len(values) - 1) // 2],
                "p95": values[max(0, math.ceil(len(values) * 0.95) - 1)], "max": values[-1]}

    total_nodes = sum(node_counts.values())
    structural_edges = internal + boundary
    return {
        "shard_count": len(shards), "files_per_shard": summary(list(files.values())),
        "nodes_per_shard": summary(list(node_counts.values())), "internal_edges": internal,
        "boundary_edges": boundary, "boundary_ratio": boundary / structural_edges if structural_edges else 0.0,
        "isolated_shards": sum(1 for shard in shards if not incident.get(shard.shard_id)),
        "giant_shard_ratio": max(node_counts.values(), default=0) / total_nodes if total_nodes else 0.0,
    }


def assign_shard(repo_id: str, relative_path: str, strategy: ShardStrategy) -> tuple[str, str]:
    path = strategy.path_for(relative_path)
    return shard_id(repo_id, path), path
