from __future__ import annotations

import hashlib
import json
import re
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .identity import edge_id, path_id, repository_id
from .models import Edge, Node, RawEvidenceLink
from .storage import SQLiteStorage


REQUIREMENT_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,5}\b")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
EXPLICIT = re.compile(r"\[(symbol|file|module|requirement):([^\]]+)\]", re.I)
BACKTICK = re.compile(r"`([^`\n]+)`")
CODE_KINDS = {"Namespace", "Class", "Struct", "Interface", "Function", "Method", "Type", "File", "Directory"}
CROSS_RELATIONS = {"DESCRIBES", "CONSTRAINS", "IMPLEMENTED_BY", "VERIFIED_BY", "BELONGS_TO", "DOCUMENTS"}


@dataclass(slots=True)
class ParsedSection:
    level: int
    title: str
    heading_path: str
    explicit_id: str | None
    start_line: int
    end_line: int
    text: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence_id(repo_id: str, source_id: str, anchor_type: str, raw: str, ordinal: int) -> str:
    return "evidence:" + _hash("\x1f".join((repo_id, source_id, anchor_type, raw, str(ordinal))))


def _sections(text: str) -> list[ParsedSection]:
    lines = text.splitlines()
    headings = [(index, len(match.group(1)), match.group(2).strip())
                for index, line in enumerate(lines, 1) if (match := HEADING.match(line))]
    result, stack = [], []
    occurrences: dict[str, int] = {}
    for position, (line, level, title) in enumerate(headings):
        stack = stack[:level - 1]
        stack.append(title)
        end = headings[position + 1][0] - 1 if position + 1 < len(headings) else len(lines)
        body = "\n".join(lines[line - 1:end])
        raw_heading_path = " / ".join(stack)
        occurrences[raw_heading_path] = occurrences.get(raw_heading_path, 0) + 1
        heading_path = (raw_heading_path if occurrences[raw_heading_path] == 1
                        else f"{raw_heading_path} [{occurrences[raw_heading_path]}]")
        # An ID mentioned in prose is evidence, not the identity of that section.
        explicit = REQUIREMENT_ID.search(title)
        result.append(ParsedSection(
            level, title, heading_path, explicit.group(0).upper() if explicit else None,
            line, end, body,
        ))
    return result


def _anchors(section: ParsedSection) -> list[tuple[str, str]]:
    result = [(kind.lower(), value.strip()) for kind, value in EXPLICIT.findall(section.text)]
    for value in BACKTICK.findall(section.text):
        value = value.strip()
        if not value or value == section.explicit_id:
            continue
        if re.search(r"\.(?:py|c|cc|cpp|h|hpp)(?::\d+)?$", value, re.I):
            result.append(("file_line" if re.search(r":\d+$", value) else "file", value))
        elif "::" in value or "." in value:
            result.append(("symbol", value))
        elif re.fullmatch(r"[A-Za-z_]\w*", value):
            result.append(("symbol", value))
    for requirement in REQUIREMENT_ID.findall(section.text):
        requirement = requirement.upper()
        if requirement != section.explicit_id:
            result.append(("requirement", requirement))
    return list(dict.fromkeys(result))


def _section_kind(path: str, section: ParsedSection) -> str:
    title = section.title.casefold()
    if section.explicit_id:
        return "Requirement"
    if "acceptance" in title or "验收" in title:
        return "AcceptanceCriterion"
    if "constraint" in title or "约束" in title:
        return "Constraint"
    if "architecture" in path.casefold() or "architecture" in title or "架构" in title:
        return "ArchitectureSection"
    return "DocumentSection"


class CrossLayerResolver:
    """Deterministic/structural evidence resolver; semantic resolution is intentionally absent."""

    def __init__(self, nodes: list[dict], shards: list[dict]) -> None:
        self.nodes, self.shards = nodes, shards
        self.requirements: dict[str, list[dict]] = defaultdict(list)
        self.files: dict[str, list[dict]] = defaultdict(list)
        self.modules: dict[str, list[dict]] = defaultdict(list)
        self.qualified_symbols: dict[str, list[dict]] = defaultdict(list)
        self.symbol_names: dict[str, list[dict]] = defaultdict(list)
        for node in nodes:
            metadata = node.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            kind = node.get("kind")
            if kind == "Requirement":
                for key in {node.get("name"), metadata.get("explicit_id")} - {None}:
                    self.requirements[key].append(node)
            if kind == "File" and metadata.get("relative_path"):
                self.files[metadata["relative_path"].replace("\\", "/")].append(node)
            if kind in {"Directory", "Namespace"}:
                for key in {node.get("qualified_name"), node.get("name")} - {None}:
                    self.modules[key].append(node)
            if kind in CODE_KINDS:
                qualified = node.get("qualified_name", "")
                self.qualified_symbols[qualified].append(node)
                self.qualified_symbols[qualified.replace(".", "::")].append(node)
                self.symbol_names[node.get("name", "")].append(node)

    def resolve(self, source: Node, raw: str, anchor_type: str, generation: int,
                ordinal: int) -> RawEvidenceLink:
        normalized = raw.replace("\\", "/")
        strategy, provenance, confidence = "unresolved", "explicit_symbol_reference", 0.0
        candidates: list[dict] = []
        if anchor_type == "requirement":
            candidates = self.requirements.get(raw, [])
            strategy, provenance, confidence = "explicit_requirement_id", "explicit_requirement_id", 1.0
        elif anchor_type in {"file", "file_line"}:
            path = normalized.rsplit(":", 1)[0] if anchor_type == "file_line" else normalized
            candidates = self.files.get(path, [])
            strategy, provenance, confidence = "exact_file_path", "explicit_file_reference", 1.0
        elif anchor_type == "module":
            candidates = self.modules.get(raw, [])
            strategy, provenance, confidence = "explicit_module_name", "explicit_module_reference", 0.9
        else:
            candidates = self.qualified_symbols.get(raw, [])
            strategy, provenance, confidence = "exact_qualified_symbol", "explicit_symbol_reference", 1.0
            if not candidates and re.fullmatch(r"[A-Za-z_]\w*", raw):
                candidates = self.symbol_names.get(raw, [])
                strategy, confidence = "symbol_name_candidate", 0.7
        ids = sorted({node["id"] for node in candidates})
        status = "resolved" if len(ids) == 1 else "ambiguous" if ids else "unresolved"
        return RawEvidenceLink(
            _evidence_id(source.repo_id, source.id, anchor_type, raw, ordinal), source.repo_id,
            source.id, raw, anchor_type, ids, ids[0] if status == "resolved" else None,
            status, strategy if ids else "unresolved", provenance,
            confidence if status == "resolved" else 0.0, generation,
            {"document_path": source.metadata["path"]},
        )


def _relation(source_kind: str, target: dict) -> str:
    if target.get("kind") == "Requirement":
        return "BELONGS_TO"
    if source_kind == "Requirement" and target.get("kind") in CODE_KINDS:
        return "IMPLEMENTED_BY"
    if source_kind in {"Constraint", "AcceptanceCriterion"}:
        return "VERIFIED_BY" if "test" in target.get("qualified_name", "").casefold() else "CONSTRAINS"
    if source_kind == "ArchitectureSection":
        return "DESCRIBES"
    return "DOCUMENTS"


# Where documents are looked for. The default is the layout the design assumed:
# a top-level `spec/`, `docs/` or `doc/`. It is a guess about a repository, and a
# guess that misses produces a knowledge layer with zero documents -- which is
# what a real checkout with `src/…/docs/` or `doc-src/` produced. The list is
# therefore configurable, and the configured list *replaces* the default one
# rather than extending it: a repository that says where its documents are has
# told us, and mixing the two would resurrect the directory that missed.
DEFAULT_DOC_DIRS = ("spec", "docs", "doc")
KNOWLEDGE_CONFIG = Path(".provenlattice") / "knowledge.toml"


def _document_roots(root: Path) -> tuple[Path, ...]:
    """The directories to scan for `*.md`, from config when the repo has one.

    Every configured path must be repository-relative: an absolute path or one
    that escapes the root with `..` is a mistake in the config, and silently
    reading outside the repository would put documents in the graph that no
    reader could locate from the repository they asked about.
    """
    config = root / KNOWLEDGE_CONFIG
    # Resolve the root here rather than trusting the caller. The containment
    # check below compares against a `.resolve()`d candidate, and on Windows a
    # path that went through a temporary directory arrives in its 8.3 short form
    # (`RUNNER~1`); the long and short spellings of the same directory are not
    # equal, so every configured path would look like it escaped the repository.
    root = Path(root).resolve()
    names = DEFAULT_DOC_DIRS
    if config.is_file():
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
        section = payload.get("documents", {})
        if not isinstance(section, dict):
            raise ValueError(f"{KNOWLEDGE_CONFIG}: [documents] must be a table")
        unknown = sorted(set(section) - {"paths"})
        if unknown:
            raise ValueError(
                f"{KNOWLEDGE_CONFIG}: [documents] has unknown keys {', '.join(unknown)}; "
                "expected paths")
        paths = section.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            raise ValueError(f"{KNOWLEDGE_CONFIG}: [documents].paths must be a list of strings")
        if any(not item.strip() for item in paths):
            raise ValueError(f"{KNOWLEDGE_CONFIG}: [documents].paths contains a blank entry")
        names = tuple(paths) if paths else DEFAULT_DOC_DIRS

    roots: list[Path] = []
    for name in names:
        candidate = (root / name).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(
                f"{KNOWLEDGE_CONFIG}: document path {name!r} is outside the repository")
        if candidate.is_dir():
            roots.append(candidate)
    return tuple(roots)


def index_knowledge(repo: str | Path, database: str | Path, *, incremental: bool = True) -> dict:
    started = time.perf_counter()
    root = Path(repo).resolve()
    repo_id = repository_id(root)
    document_roots = _document_roots(root)
    documents = sorted({path for folder in document_roots for path in folder.rglob("*.md")})
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise RuntimeError("CodeGraph must be indexed before Knowledge Layer")
        repo_id = repository["repo_id"]
        generation = int(repository["current_generation"])
        old_state = {row["path"]: dict(row) for row in storage.connection.execute(
            "SELECT * FROM document_state WHERE repository_id=?", (repo_id,))}
        hashes = {path.relative_to(root).as_posix(): _hash(path.read_text(encoding="utf-8"))
                  for path in documents}
        changed = set(hashes) if not incremental else {
            path for path, digest in hashes.items()
            if path not in old_state or old_state[path]["content_hash"] != digest}
        deleted = set(old_state) - set(hashes)
        old_sections = {row["id"]: json.loads(row["metadata"])
                        for row in storage.connection.execute(
                            "SELECT id,metadata FROM nodes WHERE json_extract(metadata,'$.layer')='knowledge'"
                        )}
        metrics = {"documents_reparsed": len(changed), "sections_reprocessed": 0,
                   "sections_reused": 0, "evidence_links_reprocessed": 0,
                   "evidence_links_reused": 0, "cross_layer_edges_added": 0,
                   "cross_layer_edges_removed": 0}
        for path in sorted(deleted):
            metrics["cross_layer_edges_removed"] += storage.connection.execute(
                "SELECT COUNT(*) FROM edges WHERE json_extract(metadata,'$.document_path')=? "
                "AND type!='CONTAINS'", (path,)
            ).fetchone()[0]
            storage.connection.execute("DELETE FROM edges WHERE json_extract(metadata,'$.document_path')=?", (path,))
            storage.connection.execute("DELETE FROM raw_evidence_links WHERE json_extract(metadata,'$.document_path')=?", (path,))
            storage.connection.execute("DELETE FROM nodes WHERE json_extract(metadata,'$.layer')='knowledge' AND json_extract(metadata,'$.path')=?", (path,))
            storage.connection.execute("DELETE FROM document_state WHERE repository_id=? AND path=?", (repo_id, path))
        for path in sorted(changed):
            storage.connection.execute("DELETE FROM document_state WHERE repository_id=? AND path=?",
                                       (repo_id, path))

        def remove_section(node_id: str) -> None:
            metrics["cross_layer_edges_removed"] += storage.connection.execute(
                "SELECT COUNT(*) FROM edges WHERE (src_id=? OR dst_id=?) "
                "AND json_extract(metadata,'$.layer')='knowledge' AND type!='CONTAINS'",
                (node_id, node_id),
            ).fetchone()[0]
            storage.connection.execute("DELETE FROM edges WHERE src_id=? OR dst_id=?",
                                       (node_id, node_id))
            storage.connection.execute("DELETE FROM raw_evidence_links WHERE source_node_id=?",
                                       (node_id,))
            storage.connection.execute("DELETE FROM nodes WHERE id=?", (node_id,))

        code_nodes = [dict(row) for row in storage.connection.execute(
            "SELECT * FROM nodes WHERE COALESCE(json_extract(metadata,'$.layer'),'code')!='knowledge'"
        )]
        shards = [dict(row) for row in storage.connection.execute("SELECT * FROM shards")]
        pending_nodes: list[Node] = []
        contains: list[Edge] = []
        parsed_by_path: dict[str, list[Node]] = {}
        for relative in sorted(changed):
            text = (root / relative).read_text(encoding="utf-8")
            digest = hashes[relative]
            doc_kind = "Spec" if relative.startswith("spec/") else "ShardDocument" if "shard" in relative.casefold() else "Document"
            document_id = path_id(repo_id, doc_kind, f"knowledge:{relative}")
            metadata = {"layer": "knowledge", "document_id": document_id,
                        "document_type": doc_kind, "path": relative, "heading_path": "",
                        "anchor": relative, "content_hash": digest,
                        "start_line": 1, "end_line": len(text.splitlines()),
                        # Content-addressed versions preserve Full(B) == Incremental(A -> B).
                        "document_version": int(digest[:12], 16)}
            document = Node(document_id, doc_kind, repo_id, None, None, Path(relative).name,
                            relative, "markdown", 1, len(text.splitlines()), None, digest,
                            generation, metadata)
            pending_nodes.append(document)
            parsed_by_path[relative] = [document]
            prior_section_ids = {
                node_id for node_id, item in old_sections.items()
                if item.get("path") == relative and item.get("heading_path")
            }
            current_section_ids: set[str] = set()
            for section in _sections(text):
                kind = _section_kind(relative, section)
                anchor = section.explicit_id or _hash(section.heading_path)[:16]
                identity = path_id(repo_id, kind, f"knowledge:{relative}#{anchor}")
                current_section_ids.add(identity)
                content_hash = _hash(section.text)
                section_metadata = {"layer": "knowledge", "document_id": document_id,
                                    "document_type": doc_kind, "path": relative,
                                    "heading_path": section.heading_path, "anchor": anchor,
                                    "explicit_id": section.explicit_id,
                                    "content_hash": content_hash,
                                    "start_line": section.start_line,
                                    "end_line": section.end_line,
                                    "document_version": metadata["document_version"],
                                    "text": section.text, "title": section.title}
                node = Node(identity, kind, repo_id, None, None,
                            section.explicit_id or section.title,
                            f"{relative}#{anchor}", "markdown", section.start_line,
                            section.end_line, None, content_hash, generation, section_metadata)
                if identity in old_sections and old_sections[identity].get("content_hash") == content_hash:
                    metrics["sections_reused"] += 1
                    storage.connection.execute(
                        "UPDATE nodes SET start_line=?,end_line=?,generation=?,metadata=? WHERE id=?",
                        (node.start_line, node.end_line, generation,
                         json.dumps(node.metadata, sort_keys=True), identity),
                    )
                    continue
                else:
                    metrics["sections_reprocessed"] += 1
                if identity in old_sections:
                    remove_section(identity)
                pending_nodes.append(node); parsed_by_path[relative].append(node)
                contains.append(Edge(edge_id(document_id, identity, "CONTAINS"), document_id,
                                     identity, "CONTAINS", "markdown_structure", 1.0,
                                     generation, {"layer": "knowledge", "document_path": relative}))
            for removed_id in sorted(prior_section_ids - current_section_ids):
                remove_section(removed_id)
            storage.connection.execute(
                "INSERT OR REPLACE INTO document_state VALUES(?,?,?,?,?,?)",
                (document_id, repo_id, relative, digest, metadata["document_version"], generation),
            )

        existing_knowledge_nodes = [dict(row) for row in storage.connection.execute(
            "SELECT * FROM nodes WHERE json_extract(metadata,'$.layer')='knowledge'"
        )]
        all_nodes = (code_nodes + existing_knowledge_nodes
                     + [node.to_dict() | {"metadata": json.dumps(node.metadata)}
                        for node in pending_nodes])
        resolver = CrossLayerResolver(all_nodes, shards)
        raw_links: list[RawEvidenceLink] = []
        cross_edges: list[Edge] = []
        targets = {node["id"]: node for node in all_nodes}
        for path, nodes in parsed_by_path.items():
            for node in nodes[1:]:
                section = ParsedSection(0, node.name, node.metadata["heading_path"],
                                        node.metadata.get("explicit_id"), node.start_line or 1,
                                        node.end_line or 1, node.metadata["text"])
                for ordinal, (anchor_type, raw) in enumerate(_anchors(section)):
                    link = resolver.resolve(node, raw, anchor_type, generation, ordinal)
                    raw_links.append(link)
                    if link.resolution_status == "resolved":
                        relation = _relation(node.kind, targets[link.resolved_target_id])
                        cross_edges.append(Edge(
                            edge_id(node.id, link.resolved_target_id, relation, link.id),
                            node.id, link.resolved_target_id, relation, link.provenance,
                            link.confidence, generation,
                            {"layer": "knowledge", "raw_evidence_link_id": link.id,
                             "document_path": path},
                        ))
        metrics["evidence_links_reprocessed"] = len(raw_links)
        metrics["cross_layer_edges_added"] = len(cross_edges)
        storage.connection.executemany(
            "INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(n.id,n.kind,n.repo_id,n.shard_id,n.file_id,n.name,n.qualified_name,n.language,
              n.start_line,n.end_line,n.signature,n.source_hash,n.generation,
              json.dumps(n.metadata,sort_keys=True)) for n in pending_nodes],
        )
        storage.connection.executemany(
            "INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?,?,?,?)",
            [(e.id,e.src_id,e.dst_id,e.type,e.provenance,e.confidence,e.generation,
              json.dumps(e.metadata,sort_keys=True)) for e in contains + cross_edges],
        )
        storage.connection.executemany(
            "INSERT INTO raw_evidence_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r.id,r.repository_id,r.source_node_id,r.raw_anchor,r.anchor_type,
              json.dumps(r.candidate_targets),r.resolved_target_id,r.resolution_status,
              r.resolution_strategy,r.provenance,r.confidence,r.generation,
              json.dumps(r.metadata,sort_keys=True)) for r in raw_links],
        )
        # A code-only incremental update can invalidate a previously resolved target
        # without changing its document. Re-resolve only stale or non-resolved evidence;
        # trustworthy links whose target still exists are reused verbatim.
        current_nodes = {node["id"]: node for node in all_nodes}
        newly_processed_evidence = {link.id for link in raw_links}
        stale_rows = [dict(row) for row in storage.connection.execute(
            "SELECT * FROM raw_evidence_links"
        ) if row["id"] not in newly_processed_evidence and (
             row["generation"] != generation
             or (row["resolved_target_id"] is not None
                 and row["resolved_target_id"] not in current_nodes)
             or (changed and row["anchor_type"] == "requirement"
                 and row["resolution_status"] != "resolved"))]
        for row in stale_rows:
            source_data = current_nodes.get(row["source_node_id"])
            if source_data is None:
                continue
            source_metadata = json.loads(source_data["metadata"] or "{}")
            source = Node(
                source_data["id"], source_data["kind"], source_data["repo_id"],
                source_data.get("shard_id"), source_data.get("file_id"), source_data["name"],
                source_data["qualified_name"], source_data.get("language"),
                source_data.get("start_line"), source_data.get("end_line"),
                source_data.get("signature"), source_data.get("source_hash"),
                source_data["generation"], source_metadata,
            )
            refreshed = resolver.resolve(source, row["raw_anchor"], row["anchor_type"],
                                         generation, 0)
            refreshed.id = row["id"]
            old_edge_count = storage.connection.execute(
                "SELECT COUNT(*) FROM edges WHERE json_extract(metadata,'$.raw_evidence_link_id')=?",
                (row["id"],),
            ).fetchone()[0]
            metrics["cross_layer_edges_removed"] += old_edge_count
            storage.connection.execute(
                "DELETE FROM edges WHERE json_extract(metadata,'$.raw_evidence_link_id')=?",
                (row["id"],),
            )
            storage.connection.execute(
                "UPDATE raw_evidence_links SET candidate_targets=?,resolved_target_id=?,"
                "resolution_status=?,resolution_strategy=?,provenance=?,confidence=?,generation=? "
                "WHERE id=?",
                (json.dumps(refreshed.candidate_targets), refreshed.resolved_target_id,
                 refreshed.resolution_status, refreshed.resolution_strategy,
                 refreshed.provenance, refreshed.confidence, generation, refreshed.id),
            )
            if refreshed.resolution_status == "resolved":
                relation = _relation(source.kind, current_nodes[refreshed.resolved_target_id])
                edge = Edge(edge_id(source.id, refreshed.resolved_target_id, relation, refreshed.id),
                            source.id, refreshed.resolved_target_id, relation,
                            refreshed.provenance, refreshed.confidence, generation,
                            {"layer": "knowledge", "raw_evidence_link_id": refreshed.id,
                             "document_path": source.metadata["path"]})
                storage.connection.execute(
                    "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)",
                    (edge.id, edge.src_id, edge.dst_id, edge.type, edge.provenance,
                     edge.confidence, edge.generation, json.dumps(edge.metadata, sort_keys=True)),
                )
                metrics["cross_layer_edges_added"] += 1
        metrics["evidence_links_reprocessed"] += len(stale_rows)
        total_evidence = storage.connection.execute("SELECT COUNT(*) FROM raw_evidence_links").fetchone()[0]
        metrics["evidence_links_reused"] = max(total_evidence - len(raw_links) - len(stale_rows), 0)
        counts = {row[0]: row[1] for row in storage.connection.execute(
            "SELECT resolution_status,COUNT(*) FROM raw_evidence_links GROUP BY resolution_status"
        )}
        metrics.update({"knowledge_nodes": storage.connection.execute(
            "SELECT COUNT(*) FROM nodes WHERE json_extract(metadata,'$.layer')='knowledge'"
                        ).fetchone()[0], "document_sections": storage.connection.execute(
            "SELECT COUNT(*) FROM nodes WHERE json_extract(metadata,'$.layer')='knowledge' "
            "AND json_extract(metadata,'$.heading_path')!=''"
        ).fetchone()[0], "requirements": storage.connection.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='Requirement'"
        ).fetchone()[0], "raw_evidence_links": total_evidence,
                        "resolved_evidence": counts.get("resolved",0),
                        "ambiguous_evidence": counts.get("ambiguous",0),
                        "unresolved_evidence": counts.get("unresolved",0),
                        "cross_layer_edges": storage.connection.execute(
                            "SELECT COUNT(*) FROM edges WHERE json_extract(metadata,'$.layer')='knowledge' AND type!='CONTAINS'"
                        ).fetchone()[0],
                        "knowledge_index_time_ms": (time.perf_counter()-started)*1000,
                        # What was scanned, so "zero documents" is diagnosable:
                        # an empty list means the repository's documents live
                        # somewhere the default (or the config) does not name.
                        "document_roots": [path.relative_to(root).as_posix() or "."
                                           for path in document_roots],
                        "documents_found": len(documents)})
        storage.connection.commit()
        return metrics
