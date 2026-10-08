# -*- coding: utf-8 -*-
"""Render the sparse CFG and the sparse DFG of an indexed repository.

    python experiments/defect_v1/view_semantic_graph.py <database> [options]

The two semantic tables are the whole graph: `semantic_events` holds the
points, `semantic_edges` holds two relations that mean different things.

  CONTROL_REACHES  the sparse CFG. One row = "control can get from this point
                   to that point". A destination of `cfg-exit:<hash>` is the
                   method's synthetic exit -- it has no event row, because an
                   exit is an edge endpoint and not an event.
  DATA_FLOW_TO     the sparse DFG. One row = "this definition reaches that
                   use". `flow_class` says which kind of value travelled and
                   `via` names the chain it travelled through, so the middle
                   of the chain is recoverable rather than implied.

Reading them together is the point: the CFG says whether a use is on a path
from a definition, the DFG says whether it is the same value. Neither answers
a defect question alone -- that is what the typed queries in `defect.py` do.

Options:
  --method NAME   only this owner (a qualified name, a bare name, or a
                  `symbol:...` id; a substring match is accepted)
  --file PATH     only events whose metadata `relative_path` is this path
  --dot           emit Graphviz DOT instead of text (pipe it to `dot -Tsvg`)
  --summary       counts only, no per-method detail
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

EXIT_PREFIX = "cfg-exit:"
CONTROL = "CONTROL_REACHES"
FLOW = "DATA_FLOW_TO"


def open_read_only(database: str) -> sqlite3.Connection:
    """Frozen databases are evidence. Never open one writable by accident."""
    connection = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load(connection: sqlite3.Connection) -> tuple[dict, list[dict]]:
    events: dict[str, dict] = {}
    for row in connection.execute("SELECT * FROM semantic_events ORDER BY id"):
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"] or "{}")
        item["flags"] = json.loads(item["flags"] or "[]")
        events[item["id"]] = item
    edges: list[dict] = []
    for row in connection.execute("SELECT * FROM semantic_edges ORDER BY id"):
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"] or "{}")
        item["flags"] = json.loads(item["flags"] or "[]")
        edges.append(item)
    return events, edges


def owner_names(connection: sqlite3.Connection) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in connection.execute(
        "SELECT id, name, qualified_name, metadata FROM nodes WHERE id LIKE 'symbol:%'"
    ):
        metadata = json.loads(row["metadata"] or "{}")
        label = row["qualified_name"] or row["name"] or row["id"]
        path = metadata.get("relative_path")
        names[row["id"]] = f"{label}  ({path})" if path else label
    return names


def label(point_id: str, index: dict[str, int], events: dict[str, dict]) -> str:
    """`#3 CHECK p@26`, or `EXIT` for the synthetic endpoint."""
    if point_id.startswith(EXIT_PREFIX):
        return "EXIT"
    event = events.get(point_id)
    if event is None:
        return f"?{point_id[:14]}"
    subject = event["metadata"].get("subject") or ""
    line = event["start_line"]
    return f"#{index[point_id]} {event['event_type']} {subject}@{line}".replace("  ", " ")


def group(events: dict[str, dict], edges: list[dict], want_method: str | None,
          want_file: str | None) -> dict[str, dict]:
    """Owner -> its points (source order) and its two edge sets."""
    owners: dict[str, dict] = {}

    def bucket(owner: str) -> dict:
        return owners.setdefault(owner, {"points": [], "control": [], "flow": []})

    for event in events.values():
        if want_file and event["metadata"].get("relative_path") != want_file:
            continue
        bucket(event["owner_symbol_id"])["points"].append(event)

    for edge in edges:
        owner = edge["owner_symbol_id"]
        if owner not in owners:
            continue
        (bucket(owner)["control"] if edge["relation"] == CONTROL
         else bucket(owner)["flow"]).append(edge)

    # Source order, not id order: an ordinal is a position among same-type
    # events in one owner, so it only orders events of one type. Line first.
    for state in owners.values():
        state["points"].sort(key=lambda e: (e["start_line"], e["ordinal"], e["id"]))
        state["index"] = {e["id"]: i + 1 for i, e in enumerate(state["points"])}

    if want_method:
        def matches(owner: str) -> bool:
            name = owner_names_cache.get(owner, "")
            return want_method in owner or want_method in name
        owners = {owner: state for owner, state in owners.items() if matches(owner)}
    return owners


def render_text(owners: dict[str, dict], events: dict[str, dict], names: dict[str, str]) -> str:
    out: list[str] = []
    for owner in sorted(owners, key=lambda o: names.get(o, o)):
        state = owners[owner]
        index = state["index"]
        out.append("")
        out.append("=" * 100)
        out.append(f"METHOD  {names.get(owner, owner)}")
        out.append(f"        {owner}")
        # ASCII only: a Windows console in cp936 draws a middle dot as noise,
        # and a viewer that mangles its own output is not a viewer.
        out.append(f"        {len(state['points'])} points | "
                   f"{len(state['control'])} CFG edges | {len(state['flow'])} DFG edges")
        out.append("=" * 100)

        out.append("")
        out.append("  POINTS (source order)")
        out.append("  %-4s %-14s %-22s %-18s %s" % ("#", "TYPE", "SUBJECT", "MATCHED", "LINE"))
        for event in state["points"]:
            metadata = event["metadata"]
            subject = metadata.get("subject") or "-"
            if metadata.get("subject_member"):
                subject = f"{subject}.{metadata['subject_member']}"
            out.append("  %-4d %-14s %-22s %-18s %d" % (
                index[event["id"]], event["event_type"], subject[:22],
                (event["matched_name"] or "-")[:18], event["start_line"]))

        out.append("")
        out.append("  CFG  CONTROL_REACHES  (control can get from -> to)")
        if not state["control"]:
            out.append("    (none)")
        for edge in sorted(state["control"], key=lambda e: (index.get(e["src_event_id"], 0),
                                                            index.get(e["dst_event_id"], 0))):
            flags = "".join(edge["flags"])
            arrow = f" --{flags}--> " if flags else " --> "
            out.append("    " + label(edge["src_event_id"], index, events)
                       + arrow + label(edge["dst_event_id"], index, events))

        out.append("")
        out.append("  DFG  DATA_FLOW_TO  (this definition reaches that use)")
        if not state["flow"]:
            out.append("    (none)")
        for edge in sorted(state["flow"], key=lambda e: (index.get(e["src_event_id"], 0),
                                                         index.get(e["dst_event_id"], 0))):
            metadata = edge["metadata"]
            via = metadata.get("via") or []
            through = ""
            if via:
                hops = " -> ".join(label(v, index, events) for v in via)
                through = f"\n         through: {hops}"
            out.append("    %s --%s--> %s%s" % (
                label(edge["src_event_id"], index, events),
                metadata.get("flow_class", "?"),
                label(edge["dst_event_id"], index, events),
                through))
            detail = []
            if metadata.get("arg_root"):
                detail.append(f"arg_root={metadata['arg_root']}")
            if metadata.get("control_reachable") is not None:
                detail.append(f"control_reachable={metadata['control_reachable']}")
            if detail:
                out.append("         " + " | ".join(detail))
    return "\n".join(out) + "\n"


def render_dot(owners: dict[str, dict], events: dict[str, dict], names: dict[str, str]) -> str:
    out = ["digraph semantic {", "  rankdir=LR;",
           '  node [shape=box, fontname="monospace", fontsize=10];',
           '  edge [fontname="monospace", fontsize=9];']
    for owner in sorted(owners, key=lambda o: names.get(o, o)):
        state = owners[owner]
        index = state["index"]
        out.append(f'  subgraph cluster_{abs(hash(owner)) % 10**8} {{')
        out.append(f'    label="{names.get(owner, owner)}";')
        for event in state["points"]:
            node = f'"{event["id"]}"'
            text = label(event["id"], index, events).replace('"', "'")
            out.append(f'    {node} [label="{text}"];')
        out.append(f'    "{EXIT_PREFIX}{owner}" [label="EXIT", shape=oval];')
        for edge in state["control"]:
            dst = edge["dst_event_id"]
            if dst.startswith(EXIT_PREFIX):
                dst = f"{EXIT_PREFIX}{owner}"
            flags = "".join(edge["flags"])
            out.append(f'    "{edge["src_event_id"]}" -> "{dst}" '
                       f'[color="#888888", label="{flags}"];')
        for edge in state["flow"]:
            dst = edge["dst_event_id"]
            if dst.startswith(EXIT_PREFIX):
                dst = f"{EXIT_PREFIX}{owner}"
            kind = edge["metadata"].get("flow_class", "?")
            out.append(f'    "{edge["src_event_id"]}" -> "{dst}" '
                       f'[color="#cc0000", penwidth=2, label="{kind}"];')
        out.append("  }")
    out.append("}")
    return "\n".join(out) + "\n"


def render_mermaid(owners: dict[str, dict], events: dict[str, dict], names: dict[str, str]) -> str:
    """A fenced ```mermaid block: VS Code's markdown preview draws this with no
    extension installed, which makes it the cheapest way to *see* the graph."""
    out = ["```mermaid", "flowchart LR"]
    for position, owner in enumerate(sorted(owners, key=lambda o: names.get(o, o))):
        state = owners[owner]
        index = state["index"]
        tag = f"m{position}"
        out.append(f'  subgraph {tag}["{names.get(owner, owner)}"]')
        for event in state["points"]:
            text = label(event["id"], index, events).replace('"', "'")
            out.append(f'    {tag}_{index[event["id"]]}["{text}"]')
        out.append(f'    {tag}_exit(["EXIT"])')
        out.append("  end")
        for edge in state["control"]:
            src = f'{tag}_{index[edge["src_event_id"]]}'
            dst = (f"{tag}_exit" if edge["dst_event_id"].startswith(EXIT_PREFIX)
                   else f'{tag}_{index[edge["dst_event_id"]]}')
            flags = "".join(edge["flags"])
            if flags:
                out.append(f'  {src} -- "{flags}" --> {dst}')
            else:
                out.append(f"  {src} --> {dst}")
        for edge in state["flow"]:
            src = f'{tag}_{index[edge["src_event_id"]]}'
            dst = (f"{tag}_exit" if edge["dst_event_id"].startswith(EXIT_PREFIX)
                   else f'{tag}_{index[edge["dst_event_id"]]}')
            kind = edge["metadata"].get("flow_class", "?")
            out.append(f'  {src} -. "{kind}" .-> {dst}')
    out.append("```")
    return "\n".join(out) + "\n"


owner_names_cache: dict[str, str] = {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="view_semantic_graph")
    parser.add_argument("database")
    parser.add_argument("--method")
    parser.add_argument("--file")
    parser.add_argument("--limit", type=int, default=20,
                        help="max methods to render (default 20; a real repo "
                             "has thousands and rendering all of them helps nobody)")
    parser.add_argument("--all", action="store_true", help="render every method")
    parser.add_argument("--dot", action="store_true")
    parser.add_argument("--mermaid", action="store_true",
                        help="emit a ```mermaid block (paste into a .md file)")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)

    # A redirected stdout would otherwise be encoded in the console's locale
    # (cp936 here), which turns a Chinese path in the output into noise -- the
    # same fix `cli.py` applies for the same reason. Interactive output keeps
    # the console's own encoding, which is the one it can actually draw.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None and not sys.stdout.isatty():
        reconfigure(encoding="utf-8")

    connection = open_read_only(args.database)
    try:
        events, edges = load(connection)
        global owner_names_cache
        owner_names_cache = owner_names(connection)
    finally:
        connection.close()

    if args.summary:
        by_relation: dict[str, int] = {}
        by_flow: dict[str, int] = {}
        for edge in edges:
            by_relation[edge["relation"]] = by_relation.get(edge["relation"], 0) + 1
            if edge["relation"] == FLOW:
                kind = edge["metadata"].get("flow_class", "?")
                by_flow[kind] = by_flow.get(kind, 0) + 1
        by_type: dict[str, int] = {}
        for event in events.values():
            by_type[event["event_type"]] = by_type.get(event["event_type"], 0) + 1
        print(f"database      {args.database}")
        print(f"events        {len(events)}")
        print(f"edges         {by_relation}")
        print(f"event types   {dict(sorted(by_type.items()))}")
        print(f"flow classes  {dict(sorted(by_flow.items()))}")
        return 0

    owners = group(events, edges, args.method, args.file)
    if not owners:
        print("nothing matched. try --summary to see what the database holds.",
              file=sys.stderr)
        return 1
    total = len(owners)
    if not args.all and args.limit and total > args.limit:
        # Deterministic truncation, and it says so: a silent cut would read as
        # "this is the whole graph", which on a real repository it never is.
        keep = sorted(owners, key=lambda o: owner_names_cache.get(o, o))[: args.limit]
        owners = {owner: owners[owner] for owner in keep}
        print(f"# showing {len(owners)} of {total} methods "
              f"(raise with --limit N or --all)", file=sys.stderr)
    if args.dot:
        print(render_dot(owners, events, owner_names_cache))
    elif args.mermaid:
        print(render_mermaid(owners, events, owner_names_cache))
    else:
        print(render_text(owners, events, owner_names_cache))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
