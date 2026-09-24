from __future__ import annotations

import hashlib
from pathlib import Path


def _stable(prefix: str, *parts: object) -> str:
    canonical = "\x1f".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def repository_id(root: Path) -> str:
    return _stable("repo", root.resolve().as_posix().casefold())


def path_id(repo_id: str, kind: str, relative_path: str) -> str:
    return _stable(kind.lower(), repo_id, relative_path.replace("\\", "/"))


def symbol_id(
    repo_id: str,
    relative_path: str,
    kind: str,
    qualified_name: str,
    signature: str,
) -> str:
    return _stable(
        "symbol",
        repo_id,
        relative_path.replace("\\", "/"),
        kind,
        qualified_name,
        signature,
    )


def edge_id(src_id: str, dst_id: str, edge_type: str, discriminator: str = "") -> str:
    return _stable("edge", src_id, dst_id, edge_type, discriminator)


def shard_id(repo_id: str, path: str) -> str:
    return _stable("shard", repo_id, path.replace("\\", "/"))


def event_id(
    repo_id: str,
    owner_symbol_id: str,
    event_type: str,
    ordinal: int,
) -> str:
    """Identity of one semantic event, anchored to its method, not its line.

    Deliberately excludes the line number, the matched keyword and the file
    path (appendix B.2.1). Any of those would make the id change when the code
    is merely reformatted, renamed or moved within the file, and an id that
    churns on reformatting destroys the incremental delta it exists to serve.
    What survives is: the same kind of operation, in the same method, in the
    same position among operations of that kind.
    """
    return _stable("event", repo_id, owner_symbol_id, event_type, ordinal)


def semantic_edge_id(
    src_event_id: str,
    dst_event_id: str,
    relation: str,
    discriminator: str = "",
) -> str:
    """Identity of one edge between two semantic events (stage 3).

    A distinct prefix from `edge_id`, so a semantic edge id can never collide
    with a code edge id. Both endpoints are method-anchored, so the id survives
    reformatting, and no line number or path takes part. The discriminator is
    the edge's flag set: two edges between the same pair of points are a real
    possibility (`branch=true` and `branch=false` out of one check), and the
    flags are what tells them apart. Equal flag sets between the same pair are
    the same reachability fact, so they collapse to one id by design.
    """
    return _stable("semantic_edge", src_event_id, dst_event_id, relation, discriminator)


CFG_EXIT_PREFIX = "cfg-exit:"


def cfg_exit_id(repo_id: str, owner_symbol_id: str) -> str:
    """Identity of a method's synthetic control-flow exit (stage 3).

    The exit is not extracted from the source -- every method has one whether
    or not a `return` appears -- so it deliberately does not live in
    `semantic_events` and carries no event ordinal. It exists so the sparse CFG
    has a sink: `void f() { char *p = malloc(8); }` has no RETURN event, and
    without a sink the most common leak shape has no path to be found on. The
    prefix is exported so the query layer can recognise these ids without
    reverse-engineering the hash.
    """
    return _stable("cfg-exit", repo_id, owner_symbol_id)


def raw_reference_id(
    repo_id: str,
    file_id: str,
    owner_symbol_id: str,
    reference_type: str,
    raw_name: str,
    ordinal: int,
) -> str:
    return _stable(
        "reference", repo_id, file_id, owner_symbol_id, reference_type, raw_name, ordinal
    )
