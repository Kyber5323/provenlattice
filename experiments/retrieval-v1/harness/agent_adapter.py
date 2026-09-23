from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from provenlattice.evidence import parse_evidence_citations

from .models import RunRequest, ToolEvent


@dataclass(slots=True)
class AdapterResult:
    output: str
    events: list[ToolEvent]
    exit_reason: str
    error: str | None = None
    actual_model: str | None = None
    actual_version: str | None = None
    backend_model: str | None = None
    permission_denials: list[dict] | None = None


class CommandAgentAdapter:
    """Product-neutral adapter for one user-selected agent executable."""

    def __init__(self, command: str | list[str]) -> None:
        self.command = shlex.split(command, posix=False) if isinstance(command, str) else command
        self.command = [part.strip('"') for part in self.command]

    def run(self, request: RunRequest, on_event: Callable[[ToolEvent], None] | None = None) -> AdapterResult:
        env = dict(request.environment)
        env.update({"PL_R1_RUN_REQUEST": json.dumps(request.to_dict(), ensure_ascii=False),
                    "PL_R1_READ_ONLY": "1"})
        command = list(self.command)
        stdin_value = json.dumps(request.to_dict(), ensure_ascii=False)
        executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if executable in {"claude", "claude.cmd", "claude.exe", "claude.ps1"}:
            # CreateProcess does not apply PowerShell's command discovery. On
            # Windows, resolve the npm-generated claude.cmd explicitly.
            command[0] = shutil.which(command[0]) or command[0]
            # Claude print mode consumes the task prompt as text. The complete
            # RunRequest remains available in PL_R1_RUN_REQUEST for auditing.
            stdin_value = request.prompt
            arm_prompt = self._claude_arm_prompt(request)
            if "--append-system-prompt" not in command:
                command.extend(["--append-system-prompt", arm_prompt])
            model_id = request.environment.get("PL_MODEL_ID")
            if model_id and model_id != "unknown" and "--model" not in command:
                command.extend(["--model", model_id])
            if "--permission-mode" not in command:
                command.extend(["--permission-mode", "dontAsk"])
            if "--allowedTools" not in command and "--allowed-tools" not in command:
                allowed = "Read,Grep,Glob"
                if request.arm != "native":
                    allowed += (",Bash(provenlattice *),Bash(provenlattice.exe *),"
                                "Bash(python -m provenlattice *)")
                command.extend(["--allowedTools", allowed])
            if "--disallowedTools" not in command and "--disallowed-tools" not in command:
                command.extend(["--disallowedTools", "Edit,Write,NotebookEdit"])
            if "--safe-mode" not in command:
                command.append("--safe-mode")
            if "--no-session-persistence" not in command:
                command.append("--no-session-persistence")
        try:
            completed = subprocess.run(
                command, cwd=request.repo_path,
                input=stdin_value, text=True, encoding="utf-8", errors="replace",
                capture_output=True, timeout=request.timeout,
                env={**__import__("os").environ, **env},
                # never allocate a visible console for unattended batch runs:
                # a manually closed console window kills the whole session tree
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            return AdapterResult(exc.stdout or "", [], "timeout", str(exc))
        except OSError as exc:
            return AdapterResult("", [], "adapter_error", str(exc))
        events: list[ToolEvent] = []
        events_by_tool_id: dict[str, ToolEvent] = {}
        output_lines: list[str] = []
        actual_model: str | None = None
        actual_version: str | None = None
        backend_model: str | None = None
        permission_denials: list[dict] = []
        for line in completed.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                output_lines.append(line)
                continue
            if value.get("event_type") == "tool":
                event = ToolEvent.from_dict(value, request)
                events.append(event)
                if on_event:
                    on_event(event)
            elif value.get("type") == "assistant":
                message_model = (value.get("message") or {}).get("model")
                if message_model and backend_model is None:
                    # the actually-served backend (gateway-resolved), as
                    # opposed to the requested alias reported by init
                    backend_model = str(message_model)
                for content in (value.get("message") or {}).get("content", []):
                    if content.get("type") != "tool_use":
                        continue
                    event = self._claude_tool_event(content, request)
                    events.append(event)
                    tool_id = str(content.get("id", ""))
                    if tool_id:
                        events_by_tool_id[tool_id] = event
                    if on_event:
                        on_event(event)
            elif value.get("type") == "user":
                for content in (value.get("message") or {}).get("content", []):
                    if content.get("type") != "tool_result":
                        continue
                    event = events_by_tool_id.get(str(content.get("tool_use_id", "")))
                    if event is None:
                        continue
                    result_text = self._tool_result_text(content.get("content"))
                    event.result_size = len(result_text)
                    structured = self._structured_result(result_text)
                    if structured:
                        ids = list(structured.get("returned_evidence_ids") or [])
                        event.query_id = structured.get("query_id")
                        event.query_type = structured.get("query_type")
                        event.anchor = structured.get("anchor")
                        event.evidence_ids = ids
                        event.viewed_evidence_ids = list(ids)
                        event.returned_evidence = int(structured.get("returned_evidence_count") or len(ids))
                        event.bundle_size = structured.get("bundle_size")
                        event.query_latency = structured.get("query_time_ms")
                        event.raw["bundle"] = structured.get("bundle")
                        # additive (D8): keep the full tool envelope so optional
                        # analysis sections survive audit without re-parsing.
                        event.raw["envelope"] = structured
                    if event.operation == "read" and isinstance(event.target, str):
                        event.files = [event.target]
            elif value.get("type") == "result":
                result_text = value.get("result")
                if isinstance(result_text, str):
                    output_lines.append(result_text)
                usage = value.get("usage") or {}
                if events and usage:
                    token_map = {
                        "input_tokens": "input", "output_tokens": "output",
                        "cache_read_input_tokens": "cache_read",
                        "cache_creation_input_tokens": "cache_creation",
                    }
                    for source, target in token_map.items():
                        if isinstance(usage.get(source), (int, float)):
                            events[-1].tokens[target] = int(usage[source])
                    events[-1].tokens["total"] = sum(events[-1].tokens.values())
            elif value.get("type") == "system":
                if value.get("subtype") == "init":
                    actual_model = value.get("model")
                    actual_version = value.get("claude_code_version")
                elif value.get("subtype") == "permission_denied":
                    permission_denials.append({
                        "tool_name": value.get("tool_name", "unknown"),
                        "tool_use_id": str(value.get("tool_use_id", "")),
                        "message": value.get("message", "denied"),
                    })
            else:
                output_lines.append(line)
        used = set(parse_evidence_citations("\n".join(output_lines)))
        for event in events:
            event.used_evidence_ids = sorted(used.intersection(event.evidence_ids))
        for denial in permission_denials:
            event = events_by_tool_id.get(denial["tool_use_id"])
            denial["operation"] = event.operation if event else "unknown"
        reason = "completed" if completed.returncode == 0 else "agent_nonzero_exit"
        error = completed.stderr.strip() or None
        return AdapterResult("\n".join(output_lines), events, reason, error,
                             actual_model, actual_version, backend_model,
                             permission_denials)

    @staticmethod
    def _tool_result_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(item.get("text", "")) if isinstance(item, dict) else str(item)
                             for item in content)
        return "" if content is None else str(content)

    @staticmethod
    def _structured_result(text: str) -> dict | None:
        decoder = json.JSONDecoder()
        for offset, character in enumerate(text):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and ("returned_evidence_ids" in value or "bundle" in value):
                return value
        return None

    @staticmethod
    def _claude_arm_prompt(request: RunRequest) -> str:
        common = (
            "This is a read-only retrieval experiment. Never edit, create, delete, move, or commit files. "
            "Do not change git state. Answer the frozen user task using only permitted retrieval capabilities."
        )
        if request.arm == "native":
            return common + " Do not invoke ProvenLattice or access any ProvenLattice database."
        database = request.environment.get("PL_R1_DATABASE", "")
        graph = (
            f" Use the frozen ProvenLattice database at {database!r}. Prefer one bounded structured query: "
            "provenlattice explain-symbol ANCHOR --database DB --json --max-evidence 12; "
            "provenlattice explain-module ANCHOR --database DB --json --max-evidence 16. "
            "Invoke each ProvenLattice query as a standalone command: do not prefix cd, use loops, "
            "chain commands, pipe, redirect, or truncate its JSON. Cite only IDs actually returned, ending the answer with "
            "'Evidence Used:' followed by one '- E-...' line per used Evidence ID."
        )
        if request.arm == "codegraph":
            return common + graph + (
                " Do not use any Knowledge command: document, evidence, related-code, cross-layer, "
                "implemented, requirements, find-related-code, or find-related-documents."
            )
        return common + graph + (
            " For document/code traversal, use: provenlattice find-related-code DOCUMENT --database DB --json "
            "--max-evidence 16; or provenlattice find-related-documents SYMBOL --database DB --json "
            "--max-evidence 16."
        )

    @staticmethod
    def _claude_tool_event(content: dict, request: RunRequest) -> ToolEvent:
        name = str(content.get("name", "unknown"))
        operation = {"read": "read", "grep": "grep", "glob": "glob",
                     "bash": "shell", "powershell": "shell"}.get(name.casefold(), name.casefold())
        inputs = content.get("input") or {}
        query = inputs.get("pattern") or inputs.get("command") or inputs.get("query")
        target = inputs.get("file_path") or inputs.get("path") or inputs.get("pattern")
        # A Claude Bash invocation of the ProvenLattice CLI is normalized to
        # its logical graph operation, so arm isolation remains auditable.
        command_text = str(inputs.get("command", ""))
        if "provenlattice" in command_text.casefold() or "python -m provenlattice" in command_text.casefold():
            words = command_text.casefold().replace("\\", " ").split()
            for candidate in ("explain-symbol", "explain-module", "find-related-code",
                              "find-related-documents", "trace-evidence",
                              "symbol", "definition", "callers", "callees", "references",
                              "dependencies", "dependents", "subgraph", "shard", "impact",
                              "document", "evidence", "related-code", "cross-layer",
                              "implemented", "requirements"):
                if candidate in words:
                    operation = candidate
                    break
        return ToolEvent.from_dict({
            "event_type": "tool", "tool": name, "operation": operation,
            "query": query, "target": target, "timestamp": content.get("timestamp"),
            "claude_tool_use_id": content.get("id"),
        }, request)


class ReplayAdapter:
    """Test-only adapter; never used to claim a real agent result."""

    def __init__(self, output: str, events: list[dict]) -> None:
        self.output, self.event_values = output, events

    def run(self, request: RunRequest, on_event: Callable[[ToolEvent], None] | None = None) -> AdapterResult:
        events = [ToolEvent.from_dict(value, request) for value in self.event_values]
        if on_event:
            for event in events:
                on_event(event)
        return AdapterResult(self.output, events, "completed")
