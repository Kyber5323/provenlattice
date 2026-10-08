# -*- coding: utf-8 -*-
"""Export a method's CFG + DFG as a self-contained Markdown file.

    python experiments/defect_v1/export_method_md.py <database> \
        --method git_win32_path_remove_namespace --out d:/tmp/g.md

Why this exists next to `view_semantic_graph.py --mermaid`: that flag emits the
bare ```mermaid block and nothing else. A bare block is not a document -- a
reader who did not run the command cannot tell what a solid arrow means versus
a dotted one, cannot see which points the nodes stand for, and cannot check the
data-flow edges' `flow_class` / `via` / `control_reachable` at all. This writes
the block plus the context that makes it readable, and records the command that
produced it so the file can be regenerated.

The rendering itself is imported from the viewer rather than reimplemented, so
the two can never drift.

Options mirror the viewer's:
  --method NAME   substring match on the owner id or its display name
  --file PATH     exact match on the event's `relative_path`
  --limit N       max methods to export (default 5 -- mermaid gets unreadable
                  long before a repo runs out of methods)
  --all           export every matching method
  --out PATH      write here instead of stdout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import view_semantic_graph  # noqa: E402
from view_semantic_graph import (  # noqa: E402
    group,
    label,
    load,
    open_read_only,
    owner_names,
    render_mermaid,
)

# Past this many nodes a `flowchart LR` stops being a diagram and becomes a
# hairball. The export still happens -- the caller asked for it -- but the file
# says so, because a reader who cannot see the graph cannot tell.
UNREADABLE_POINTS = 400


def md_escape(text: str) -> str:
    """Pipes break a Markdown table cell; backticks break inline code."""
    return text.replace("|", "\\|").replace("`", "'").strip()


def render_markdown(owners: dict, events: dict, names: dict, source: str,
                    invocation: str) -> str:
    out: list[str] = []
    total_points = sum(len(s["points"]) for s in owners.values())

    if len(owners) == 1:
        only = next(iter(owners))
        out.append(f"# {names.get(only, only)}")
    else:
        out.append(f"# 语义图导出（{len(owners)} 个方法）")
    out.append("")
    out.append(f"> 由 ProvenLattice 语义图导出 · 源库 `{source}`")
    out.append("")

    if total_points > UNREADABLE_POINTS:
        out.append(
            f"> ⚠️ **{total_points} 个节点** —— 超过 {UNREADABLE_POINTS} 后 "
            f"`flowchart LR` 基本读不出结构。用 `--limit 1` 或更精确的 "
            f"`--method` 缩小范围。")
        out.append("")

    # One summary table for every owner, so a multi-method export still has an
    # index at the top rather than only a wall of diagrams.
    out.append("| 方法 | 点 | CFG 边 | DFG 边 |")
    out.append("|---|---:|---:|---:|")
    for owner in sorted(owners, key=lambda o: names.get(o, o)):
        state = owners[owner]
        out.append(f"| `{md_escape(names.get(owner, owner))}` "
                   f"| {len(state['points'])} "
                   f"| {len(state['control'])} "
                   f"| {len(state['flow'])} |")
    out.append("")

    out.append("## 图")
    out.append("")
    out.append(render_mermaid(owners, events, names).rstrip("\n"))
    out.append("")

    out.append("## 图例")
    out.append("")
    out.append("| 线型 | 关系 | 含义 |")
    out.append("|---|---|---|")
    out.append("| 实线 `-->` | `CONTROL_REACHES` | 稀疏 CFG：控制流可以从这个点走到那个点 |")
    out.append("| 实线带标签 `--\"FLAG\"-->` | `CONTROL_REACHES` | 带分支标志的控制流边"
               "（`BRANCH_TRUE` / `BRANCH_FALSE`） |")
    out.append("| 虚线 `-. \"flow_class\" .->` | `DATA_FLOW_TO` | 稀疏 DFG："
               "这个定义到达那个使用 |")
    out.append("| `EXIT` | — | 方法出口的合成端点，不是事件，只是边的端点 |")
    out.append("")
    out.append("节点标签格式：`#序号 事件类型 主体@行号`。序号是**同类型事件在"
               "该 owner 内的位置**，所以它排序的是同一类事件，跨类型不可比 —— "
               "排序以行号为准。")
    out.append("")

    for owner in sorted(owners, key=lambda o: names.get(o, o)):
        state = owners[owner]
        index = state["index"]
        out.append(f"## `{md_escape(names.get(owner, owner))}`")
        out.append("")
        out.append(f"`{owner}`")
        out.append("")

        out.append("### 点（源码顺序）")
        out.append("")
        out.append("| # | 类型 | 主体 | 匹配名 | 行 |")
        out.append("|---:|---|---|---|---:|")
        for event in state["points"]:
            metadata = event["metadata"]
            subject = metadata.get("subject") or "—"
            if metadata.get("subject_member"):
                subject = f"{subject}.{metadata['subject_member']}"
            out.append(
                f"| {index[event['id']]} "
                f"| `{event['event_type']}` "
                f"| `{md_escape(subject)}` "
                f"| `{md_escape(event['matched_name'] or '—')}` "
                f"| {event['start_line']} |")
        out.append("")

        out.append("### 数据流边")
        out.append("")
        if not state["flow"]:
            out.append("（无）")
            out.append("")
        else:
            out.append("| 从 | 到 | `flow_class` | `via`（中间链） | "
                       "`control_reachable` |")
            out.append("|---|---|---|---|---|")
            for edge in sorted(
                    state["flow"],
                    key=lambda e: (index.get(e["src_event_id"], 0),
                                   index.get(e["dst_event_id"], 0))):
                metadata = edge["metadata"]
                via = metadata.get("via") or []
                hops = " → ".join(
                    md_escape(label(v, index, events)) for v in via) or "—"
                reach = metadata.get("control_reachable")
                out.append(
                    f"| `{md_escape(label(edge['src_event_id'], index, events))}` "
                    f"| `{md_escape(label(edge['dst_event_id'], index, events))}` "
                    f"| `{metadata.get('flow_class', '?')}` "
                    f"| {hops} "
                    f"| {'—' if reach is None else f'`{reach}`'} |")
            out.append("")

    out.append("---")
    out.append("")
    out.append("复现：")
    out.append("")
    out.append("```bash")
    out.append(invocation)
    out.append("```")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_method_md")
    parser.add_argument("database")
    parser.add_argument("--method")
    parser.add_argument("--file")
    parser.add_argument("--limit", type=int, default=5,
                        help="max methods to export (default 5)")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--out", help="write here instead of stdout")
    args = parser.parse_args(argv)

    connection = open_read_only(args.database)
    try:
        events, edges = load(connection)
        names = owner_names(connection)
    finally:
        connection.close()

    # `group()` matches --method against a module-level cache that only
    # `view_semantic_graph.main()` populates. Importing the function without
    # seeding that cache makes every match fail silently -- an owner id is a
    # bare hash (`symbol:3ac97...`), so the name is the only thing a caller
    # would ever match on, and an empty cache means "no method matched".
    view_semantic_graph.owner_names_cache = names

    owners = group(events, edges, args.method, args.file)
    if not owners:
        print("no method matched; nothing written", file=sys.stderr)
        return 1

    matched = len(owners)
    if not args.all and matched > args.limit:
        ordered = sorted(owners, key=lambda o: names.get(o, o))
        owners = {o: owners[o] for o in ordered[:args.limit]}
        print(f"# showing {len(owners)} of {matched} methods "
              f"(raise with --limit N or --all)", file=sys.stderr)

    invocation = (
        f"python experiments/defect_v1/export_method_md.py {args.database}"
        + (f" --method {args.method}" if args.method else "")
        + (f" --file {args.file}" if args.file else "")
        + (f" --out {args.out}" if args.out else ""))

    document = render_markdown(owners, events, names, args.database, invocation)

    if args.out:
        Path(args.out).write_text(document, encoding="utf-8")
        print(f"wrote {args.out}  ({len(document.splitlines())} lines)", file=sys.stderr)
    else:
        sys.stdout.write(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
