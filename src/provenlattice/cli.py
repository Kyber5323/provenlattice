from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from pprint import pprint

from .graph import full_index
from .incremental import incremental_update
from .knowledge import index_knowledge
from .analysis_query import query_analysis
from .orchestrator import AnalysisRequestError, analyze_scope
from .query import GraphQuery
from .shard import BuildAwareShardStrategy, DirectoryShardStrategy, StructuralShardStrategy


ANALYSIS_FEATURES_HELP = (
    "Optional derived function analysis; use ast for function structure and call "
    "locations, or cfg for branches, loops, fallthrough, and early returns. "
    "Use comma-separated features. Omit to keep base evidence unchanged."
)


def _add_analysis_options(command: argparse.ArgumentParser, *, multi_target: bool) -> None:
    command.add_argument("--analysis", metavar="FEATURES", default=None,
                         help=ANALYSIS_FEATURES_HELP)
    if multi_target:
        command.add_argument(
            "--analysis-symbol-id", metavar="SYMBOL_ID", default=None,
            help=("Required with --analysis: exact symbol_id from this command's returned "
                  "bundle. Only that symbol is analyzed; results are never analyzed automatically."),
        )

def _database(args: argparse.Namespace) -> Path:
    if args.database:
        return Path(args.database)
    repo = Path(getattr(args, "repo", ".")).resolve()
    return repo / ".provenlattice" / "codegraph.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="provenlattice")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        child = subparsers.add_parser(name)
        child.add_argument("--database")
        child.add_argument("--json", action="store_true")
        return child

    index = command("index")
    index.add_argument("repo")
    index.add_argument("--shard-strategy", choices=("directory", "build-aware", "structural"), default="directory")
    update = command("update")
    update.add_argument("repo", nargs="?", default=".")
    update.add_argument("--shard-strategy", choices=("directory", "build-aware", "structural"), default="directory")
    knowledge = command("knowledge")
    knowledge.add_argument("repo", nargs="?", default=".")
    knowledge.add_argument("--full", action="store_true", help="rebuild all knowledge documents")
    analyze = command("analyze")
    analyze.add_argument("repo")
    analyze.add_argument("--scope", required=True)
    analyze.add_argument("--features", required=True)
    analysis_query = command("analysis")
    analysis_query.add_argument("repo")
    analysis_query.add_argument("--feature", required=True)
    analysis_selectors = analysis_query.add_mutually_exclusive_group(required=True)
    analysis_selectors.add_argument("--symbol-id")
    analysis_selectors.add_argument("--file-path")
    command("status")
    symbol = command("symbol")
    symbol.add_argument("name")
    definition = command("definition")
    definition.add_argument("symbol")
    callers = command("callers")
    callers.add_argument("symbol")
    callees = command("callees")
    callees.add_argument("symbol")
    references = command("references")
    references.add_argument("symbol")
    dependencies = command("dependencies")
    dependencies.add_argument("shard")
    dependents = command("dependents")
    dependents.add_argument("shard")
    document = command("document")
    document.add_argument("anchor")
    implemented = command("implemented")
    implemented.add_argument("requirement")
    requirements = command("requirements")
    requirements.add_argument("symbol")
    evidence = command("evidence")
    evidence.add_argument("--status", choices=("resolved", "ambiguous", "unresolved"))
    shard = command("shard")
    shard.add_argument("name")
    subgraph = command("subgraph")
    subgraph.add_argument("anchor")
    subgraph.add_argument("--max-hops", type=int, default=2)
    subgraph.add_argument("--max-nodes", type=int, default=100)

    def evidence_command(name: str, aliases: list[str] | None = None) -> argparse.ArgumentParser:
        child = subparsers.add_parser(name, aliases=aliases or [])
        child.add_argument("anchor")
        child.add_argument("--database")
        child.add_argument("--json", action="store_true")
        child.add_argument("--max-evidence", type=int, default=20)
        child.add_argument("--max-symbols", type=int, default=8)
        child.add_argument("--max-edges", type=int, default=12)
        child.add_argument("--max-sections", type=int, default=6)
        child.add_argument("--max-primary", type=int)
        child.add_argument("--max-supporting", type=int)
        child.add_argument("--max-total", type=int)
        child.add_argument("--bundle-profile", choices=(
            "generic", "document_to_code", "code_to_document", "module_understanding"),
            default=None)
        child.add_argument("--focus-term", action="append", default=[],
                           help="task-specific term used only for bundle ranking; repeatable")
        return child

    explain_symbol_cmd = evidence_command("explain-symbol", ["explain_symbol"])
    _add_analysis_options(explain_symbol_cmd, multi_target=False)
    evidence_command("explain-module", ["explain_module"])
    find_related_code_cmd = evidence_command("find-related-code", ["find_related_code"])
    _add_analysis_options(find_related_code_cmd, multi_target=True)
    find_related_documents_cmd = evidence_command(
        "find-related-documents", ["find_related_documents"])
    _add_analysis_options(find_related_documents_cmd, multi_target=False)
    evidence_command("trace-evidence", ["trace_evidence"])
    task_context = command("task-context")
    task_context.add_argument("symbol")
    task_context.add_argument("--expected-base-commit")
    return parser


def run(args: argparse.Namespace) -> dict:
    database = _database(args)
    strategy_name = getattr(args, "shard_strategy", "directory")
    strategy = {
        "directory": DirectoryShardStrategy,
        "build-aware": BuildAwareShardStrategy,
        "structural": StructuralShardStrategy,
    }[strategy_name]()
    if args.command == "index":
        return full_index(args.repo, database, strategy=strategy)
    if args.command == "update":
        return incremental_update(args.repo, database, strategy=strategy)
    if args.command == "knowledge":
        return index_knowledge(args.repo, database, incremental=not args.full)
    if args.command == "analyze":
        try:
            return analyze_scope(args.repo, args.scope, args.features, database)
        except AnalysisRequestError as error:
            return {
                "status": "failed",
                "error": {"code": error.code, "detail": error.detail},
                "scope": args.scope,
                "requested_features": [
                    item.strip() for item in args.features.split(",") if item.strip()
                ],
            }
    if args.command == "analysis":
        try:
            return query_analysis(
                args.repo, args.feature,
                file_path=args.file_path, symbol_id=args.symbol_id, database=database,
            )
        except AnalysisRequestError as error:
            return {
                "status": "failed",
                "error": {"code": error.code, "detail": error.detail},
                "feature": args.feature,
                "selector": {"symbol_id": args.symbol_id, "file_path": args.file_path},
            }
    if args.command == "task-context":
        if not database.exists():
            return {"version": "task-context-v0", "status": "not_built", "anchor": args.symbol}
        try:
            with GraphQuery(database) as query:
                result = query.task_context(args.symbol)
        except sqlite3.DatabaseError:
            return {"version": "task-context-v0", "status": "corrupt", "anchor": args.symbol}
        expected = args.expected_base_commit
        if expected and result.get("status") == "available" and result.get("base_commit") != expected:
            return {"version": "task-context-v0", "status": "stale", "anchor": args.symbol,
                    "expected_base_commit": expected, "actual_base_commit": result.get("base_commit")}
        return result
    with GraphQuery(database) as query:
        if args.command == "status":
            return query.status()
        if args.command == "symbol":
            return query.find_symbol(args.name)
        if args.command == "definition":
            return query.get_definition(args.symbol)
        if args.command == "callers":
            return query.get_callers(args.symbol)
        if args.command == "callees":
            return query.get_callees(args.symbol)
        if args.command == "references":
            return query.get_references(args.symbol)
        if args.command == "dependencies":
            return query.get_dependencies(args.shard)
        if args.command == "dependents":
            return query.get_dependents(args.shard)
        if args.command == "document":
            return query.get_document_targets(args.anchor)
        if args.command == "implemented":
            return query.get_implemented_code(args.requirement)
        if args.command == "requirements":
            return query.get_requirements(args.symbol)
        if args.command == "evidence":
            return query.get_evidence_links(status=args.status)
        if args.command == "shard":
            return {
                "graph_generation": query.graph_generation,
                "shard": query.storage.row(
                    "SELECT * FROM shards WHERE shard_id = ? OR path = ? LIMIT 1",
                    (args.name, args.name),
                ),
                "dependencies": query.get_dependencies(args.name)["data"],
                "dependents": query.get_dependents(args.name)["data"],
            }
        if args.command == "subgraph":
            return query.get_subgraph(args.anchor, args.max_hops, args.max_nodes)
        budget = {"max_evidence": args.max_evidence, "max_symbols": args.max_symbols,
                  "max_edges": args.max_edges, "max_sections": args.max_sections}
        def configured(name: str, explicit):
            return explicit if explicit is not None else os.environ.get(name)
        bundle_options = {"max_primary": configured("PL_R2_4_MAX_PRIMARY", args.max_primary),
                          "max_supporting": configured("PL_R2_4_MAX_SUPPORTING", args.max_supporting),
                          "max_total": configured("PL_R2_4_MAX_TOTAL", args.max_total),
                          "bundle_profile": configured("PL_R2_4_BUNDLE_PROFILE", args.bundle_profile) or "generic",
                          "focus_terms": tuple(args.focus_term) or tuple(filter(None, os.environ.get("PL_R2_4_FOCUS_TERMS", "").split("\x1f")))}
        for key in ("max_primary", "max_supporting", "max_total"):
            if isinstance(bundle_options[key], str): bundle_options[key] = int(bundle_options[key])
        if args.command in {"explain-symbol", "explain_symbol"}:
            return query.explain_symbol(args.anchor, **budget, **bundle_options,
                                        analysis=args.analysis)
        if args.command in {"explain-module", "explain_module"}:
            return query.explain_module(args.anchor, **budget, **bundle_options)
        if args.command in {"find-related-code", "find_related_code"}:
            return query.find_related_code(
                args.anchor, **budget, **bundle_options, analysis=args.analysis,
                analysis_symbol_id=args.analysis_symbol_id)
        if args.command in {"find-related-documents", "find_related_documents"}:
            return query.find_related_documents(args.anchor, **budget, **bundle_options,
                                                analysis=args.analysis)
        if args.command in {"trace-evidence", "trace_evidence"}:
            return query.trace_evidence(args.anchor, **budget)
    raise ValueError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        pprint(result, sort_dicts=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
