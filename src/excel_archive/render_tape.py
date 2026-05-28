"""Render session events as LLM-optimized Markdown (primary human/LLM view)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .snip import SNIP_MARKER, is_snipped_content
from .config_excel import parse_config_dict, ExcelArchiveConfig

ID_TAG_RE = re.compile(r"\[id:([a-z0-9]+)\]")

# For tape readability: large blocks are preserved but wrapped in <details>.
# These thresholds decide *when to collapse*, not when to truncate.
COLLAPSE_TOOL_RESULT_CHARS = 1200
COLLAPSE_TOOL_CODE_CHARS = 800
COLLAPSE_JSON_BLOCK_CHARS = 1200

# For the *truncated* tape variant.
TRUNCATE_TOOL_RESULT_CHARS = 1200
TRUNCATE_TOOL_CODE_CHARS = 800


def _short_ts(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return iso[:19] if iso else ""


def _message_id(text: str) -> str | None:
    m = ID_TAG_RE.search(text or "")
    return f"id:{m.group(1)}" if m else None


def _classify_user_message(text: str) -> str:
    t = text.strip()
    if t.startswith("<user_context>"):
        return "context"
    if t.startswith("<conductor_context>"):
        return "conductor"
    if t.startswith("<connected_peers>"):
        return "peers"
    if t.startswith("<user_changes>"):
        return "changes"
    if t.startswith("<uploaded_files>"):
        return "upload"
    if t.startswith("[id:") and len(t) < 24:
        return "id-tag"
    if SNIP_MARKER in t:
        return "snipped"
    return "user"


def _md_escape_block(text: str) -> str:
    return text.replace("```", "'''")


def _details(summary: str, body_lines: list[str]) -> list[str]:
    """
    HTML details block for Markdown renderers (GitHub, many viewers).
    Keeps content present for LLMs but folded for humans.
    """
    lines: list[str] = ["<details>", f"<summary>{summary}</summary>", ""]
    lines.extend(body_lines)
    lines.extend(["", "</details>"])
    return lines


def _peer_summary(events: list[dict[str, Any]]) -> list[str]:
    """
    Build a peer-linking section from:
    - send_message tool_use inputs (agent_id)
    - conductor_context / connected_peers message blocks
    """
    send_turns: dict[str, list[int]] = {}
    conductor_blocks = 0
    peers_blocks = 0

    for ev in events:
        if ev.get("kind") == "tool_use" and ev.get("name") == "send_message":
            inp = ev.get("input") or {}
            agent_id = inp.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                send_turns.setdefault(agent_id, []).append(int(ev.get("seq") or 0))
        if ev.get("kind") == "message":
            text = ev.get("text") or ""
            mclass = ev.get("message_class") or _classify_user_message(text)
            if mclass == "conductor":
                conductor_blocks += 1
            elif mclass == "peers":
                peers_blocks += 1

    if not send_turns and conductor_blocks == 0 and peers_blocks == 0:
        return []

    lines: list[str] = []
    lines.append("## Peers")
    lines.append("")
    lines.append("```mermaid")
    lines.append("flowchart LR")
    lines.append('  localSession["Local session"] -->|"send_message(agent_id)"| peerAgent["Peer agent"]')
    lines.append('  peerAgent -->|"conductor update"| localSession')
    lines.append("```")
    lines.append("")
    if peers_blocks:
        lines.append(f"- **Connected peers blocks**: {peers_blocks}")
    if conductor_blocks:
        lines.append(f"- **Conductor updates**: {conductor_blocks}")
    if send_turns:
        lines.append("")
        lines.append("### send_message by agent")
        lines.append("")
        for agent_id in sorted(send_turns.keys()):
            turns = [t for t in send_turns[agent_id] if t]
            turns_txt = ", ".join(f"Turn {t}" for t in turns[:10]) + ("…" if len(turns) > 10 else "")
            lines.append(f"- **`{agent_id}`**: {len(turns)} message(s)" + (f" ({turns_txt})" if turns_txt else ""))
    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def _format_tool_input(
    inp: dict[str, Any],
    *,
    mode: str,
    truncate_tool_code_chars: int,
) -> list[str]:
    lines: list[str] = []
    if not inp:
        return ["- *(empty input in source)*"]
    if "sheetName" in inp:
        lines.append(f"- **Sheet:** {inp['sheetName']}")
    if "ranges" in inp:
        rs = inp["ranges"]
        if isinstance(rs, list):
            lines.append(f"- **Ranges:** `{', '.join(str(r) for r in rs[:12])}`")
    if "cells" in inp and isinstance(inp["cells"], dict):
        lines.append(f"- **Cells:** {len(inp['cells'])} cell(s)")
    if "code" in inp:
        code = str(inp["code"]).strip()
        lines.append(f"- **Code:** ({len(code)} chars)")
        if mode == "full":
            if len(code) > COLLAPSE_TOOL_CODE_CHARS:
                lines.extend(
                    _details(
                        f"Show code ({len(code):,} chars)",
                        ["```javascript", code, "```"],
                    )
                )
            else:
                lines.append("```javascript")
                lines.append(code)
                lines.append("```")
        else:
            # truncated
            if len(code) > truncate_tool_code_chars:
                excerpt = code[:truncate_tool_code_chars] + "\n…"
                lines.extend(
                    _details(
                        f"Show code excerpt ({truncate_tool_code_chars:,}/{len(code):,} chars)",
                        ["```javascript", excerpt, "```"],
                    )
                )
            else:
                lines.append("```javascript")
                lines.append(code)
                lines.append("```")
    if "message" in inp:
        msg = str(inp["message"])
        lines.append(f"- **Message:** {msg[:500]}{'…' if len(msg) > 500 else ''}")
    if "agent_id" in inp:
        lines.append(f"- **Agent:** `{inp['agent_id']}`")
    if "explanation" in inp:
        lines.append(f"- **Why:** {inp['explanation']}")
    if "from_id" in inp or "fromId" in inp:
        lines.append(
            f"- **Snip range:** `{inp.get('from_id') or inp.get('fromId')}` → "
            f"`{inp.get('to_id') or inp.get('toId')}`"
        )
    if "summary" in inp:
        s = str(inp["summary"])
        lines.append(f"- **Summary:** {s[:600]}{'…' if len(s) > 600 else ''}")
    shown = {
        "sheetName", "ranges", "cells", "code", "message", "agent_id",
        "explanation", "from_id", "to_id", "fromId", "toId", "summary",
    }
    extra = {k: v for k, v in inp.items() if k not in shown}
    if extra:
        blob = json.dumps(extra, ensure_ascii=False)
        if len(blob) > 400:
            blob = blob[:397] + "…"
        lines.append(f"- **Other:** `{blob}`")
    return lines or ["- *(see input artifact)*"]


def _format_tool_result(
    content: str,
    artifacts_dir: Path | None,
    tool_id: str,
    *,
    mode: str,
    truncate_tool_result_chars: int,
) -> list[str]:
    if is_snipped_content(content):
        return ["> *Tool result snipped in source — not stored in archive body.*"]
    is_jsonish = content.strip().startswith("{") or content.strip().startswith("[")
    fence = "json" if is_jsonish else ""

    if mode == "full":
        if len(content) > COLLAPSE_TOOL_RESULT_CHARS:
            body = [f"```{fence}".rstrip(), content, "```"]
            return _details(f"Show tool result ({len(content):,} chars)", body)
        if is_jsonish and len(content) > COLLAPSE_JSON_BLOCK_CHARS:
            return _details(f"Show JSON ({len(content):,} chars)", ["```json", content, "```"])
        if is_jsonish:
            return ["```json", content, "```"]
        return [content]

    # truncated tape: keep it readable, but also preserve full payload via artifacts.
    if len(content) > truncate_tool_result_chars:
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = artifacts_dir / f"{tool_id}.txt"
            # Always write the full payload for retrieval.
            if not path.is_file():
                path.write_text(content, encoding="utf-8")
            link = f"artifacts/{path.name}"
            excerpt = content[:truncate_tool_result_chars] + "\n…"
            body = [f"```{fence}".rstrip(), excerpt, "```"] if is_jsonish else ["```", excerpt, "```"]
            return [
                f"*(result {len(content):,} chars — full payload in `{link}`)*",
                "",
                *_details(f"Show excerpt ({truncate_tool_result_chars:,}/{len(content):,} chars)", body),
            ]
        excerpt = content[:truncate_tool_result_chars] + "\n…"
        body = [f"```{fence}".rstrip(), excerpt, "```"] if is_jsonish else ["```", excerpt, "```"]
        return _details(f"Show excerpt ({truncate_tool_result_chars:,}/{len(content):,} chars)", body)

    if is_jsonish:
        return ["```json", content, "```"]
    return [content]


def export_json_to_events(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Claude for Excel export JSON to ordered tape events."""
    events: list[dict[str, Any]] = []
    seq = 0
    for msg in data.get("messages") or []:
        role = str(msg.get("role", "unknown"))
        for block in msg.get("content") or []:
            btype = block.get("type")
            seq += 1
            base = {"seq": seq, "role": role, "source": "export"}
            if btype == "text":
                text = block.get("text") or block.get("content") or ""
                if not str(text).strip():
                    continue
                events.append(
                    {
                        **base,
                        "kind": "message",
                        "message_id": _message_id(str(text)),
                        "message_class": _classify_user_message(str(text)),
                        "text": str(text),
                    }
                )
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name == "context_snip" and inp:
                    events.append(
                        {
                            **base,
                            "kind": "snip_note",
                            "registration_tool_id": block.get("id"),
                            "from_id": inp.get("from_id"),
                            "to_id": inp.get("to_id"),
                            "summary": inp.get("summary") or "",
                            "status": "registered",
                        }
                    )
                elif name in ("context_snip", "retrieve_snipped"):
                    continue
                else:
                    events.append(
                        {
                            **base,
                            "kind": "tool_use",
                            "id": block.get("id"),
                            "name": name,
                            "input": inp,
                        }
                    )
            elif btype == "tool_result":
                c = block.get("content")
                content = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                events.append(
                    {
                        **base,
                        "kind": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "content": content,
                    }
                )
    return events


def flatten_journal_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Build ordered event list from journal state.json buckets."""
    events: list[dict[str, Any]] = []
    for bucket in ("messages", "snip_notes", "tool_uses", "tool_results"):
        for item in (state.get(bucket) or {}).values():
            events.append(dict(item))
    # Stable order: by captured_at then by snapshot/seq.
    events.sort(key=lambda e: (e.get("captured_at") or "", e.get("snapshot") or "", e.get("seq") or 0))
    for i, ev in enumerate(events, start=1):
        ev.setdefault("seq", i)
    return events


def load_events_from_session(session_dir: Path) -> list[dict[str, Any]]:
    state_path = session_dir / "state.json"
    if state_path.is_file():
        return flatten_journal_state(json.loads(state_path.read_text(encoding="utf-8")))
    events: list[dict[str, Any]] = []
    jsonl = session_dir / "events.jsonl"
    if jsonl.is_file():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def render_tape_markdown(
    events: list[dict[str, Any]],
    *,
    title: str = "Claude for Excel — session tape",
    meta: dict[str, Any] | None = None,
    artifacts_dir: Path | None = None,
    source_note: str | None = None,
    mode: str = "truncated",
    truncate_tool_result_chars: int | None = None,
    truncate_tool_code_chars: int | None = None,
) -> str:
    """Render chronological Markdown tape for LLM decomposition."""
    if mode not in ("truncated", "full"):
        raise ValueError(f"unknown mode: {mode}")
    trunc_result = int(truncate_tool_result_chars or TRUNCATE_TOOL_RESULT_CHARS)
    trunc_code = int(truncate_tool_code_chars or TRUNCATE_TOOL_CODE_CHARS)
    lines: list[str] = [
        f"# {title}",
        "",
    ]
    meta = meta or {}
    if meta:
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        for k in ("surface", "platform", "officeVersion", "exportedAt", "vendor"):
            if meta.get(k):
                lines.append(f"| {k} | {meta[k]} |")
        lines.append("")

    if source_note:
        lines.append(f"> {source_note}")
        lines.append("")

    lines.extend(_peer_summary(events))

    lines.append("---")
    lines.append("")

    tool_inputs: dict[str, dict] = {}
    turn = 0
    for ev in events:
        kind = ev.get("kind")
        ts = _short_ts(ev.get("captured_at"))
        ts_part = f" · {ts}" if ts else ""

        if kind == "message":
            turn += 1
            role = ev.get("role", "?")
            mclass = ev.get("message_class") or _classify_user_message(ev.get("text", ""))
            mid = ev.get("message_id")
            label = role.title()
            if mclass not in ("user", "assistant", "snipped"):
                label = f"{label} · {mclass}"
            mid_part = f" · `{mid}`" if mid else ""
            lines.append(f"## Turn {turn}{ts_part} · {label}{mid_part}")
            lines.append("")

            text = ev.get("text", "")
            if mclass == "snipped" or SNIP_MARKER in text:
                lines.append("> **Snipped in live session**")
                lines.append(">")
                if text.replace(SNIP_MARKER, "").strip():
                    body = text.replace(SNIP_MARKER, "").strip()
                    for para in body.split("\n\n")[:3]:
                        lines.append(f"> {_md_escape_block(para[:500])}")
                else:
                    lines.append("> *(placeholder only)*")
            elif mclass in ("context", "conductor", "peers", "changes", "upload", "id-tag"):
                lines.append(f"```\n{text.strip()}\n```")
            else:
                lines.append(_md_escape_block(text.strip()))
            lines.append("")

        elif kind == "snip_note":
            turn += 1
            fid = ev.get("from_id") or "?"
            tid = ev.get("to_id") or "?"
            lines.append(f"## Turn {turn}{ts_part} · Context snip registered")
            lines.append("")
            lines.append(f"> **Range:** `{fid}` → `{tid}`")
            if ev.get("registration_tool_id"):
                lines.append(f"> **Tool id:** `{ev['registration_tool_id']}`")
            summary = (ev.get("summary") or "").strip()
            if summary:
                lines.append(">")
                for para in summary.split("\n\n")[:4]:
                    lines.append(f"> {_md_escape_block(para)}")
            lines.append("")

        elif kind == "tool_use":
            tid = ev.get("id") or "?"
            name = ev.get("name") or "tool"
            tool_inputs[tid] = ev.get("input") or {}
            lines.append(f"### Tool · {name} · `{tid}`{ts_part}")
            lines.append("")
            lines.extend(_format_tool_input(tool_inputs[tid], mode=mode, truncate_tool_code_chars=trunc_code))
            lines.append("")

        elif kind == "tool_result":
            tuid = ev.get("tool_use_id") or "?"
            lines.append(f"**Result** · `{tuid}`{ts_part}")
            lines.append("")
            content = ev.get("content") or ""
            lines.extend(
                _format_tool_result(
                    content,
                    artifacts_dir,
                    tuid,
                    mode=mode,
                    truncate_tool_result_chars=trunc_result,
                )
            )
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"*Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} "
        f"· {len(events)} events · excel-archive*"
    )
    return "\n".join(lines)


def write_session_tape(
    session_dir: Path,
    *,
    title: str | None = None,
    meta: dict[str, Any] | None = None,
    source_note: str | None = None,
) -> Path:
    """Regenerate session.tape.md and session.full.tape.md from journal state."""
    session_dir.mkdir(parents=True, exist_ok=True)
    events = load_events_from_session(session_dir)
    artifacts = session_dir / "artifacts"
    # Load per-workbook config if we’re under <workbook_root>/journal/<session>/.
    cfg = ExcelArchiveConfig()
    try:
        workbook_root = session_dir.parent.parent
        cfg_path = workbook_root / "excel-archive.json"
        if cfg_path.is_file():
            cfg = parse_config_dict(json.loads(cfg_path.read_text(encoding="utf-8")))
    except Exception:
        pass
    md_trunc = render_tape_markdown(
        events,
        title=title or f"Session — {session_dir.name}",
        meta=meta,
        artifacts_dir=artifacts if any(e.get("kind") == "tool_result" for e in events) else None,
        source_note=source_note,
        mode="truncated",
        truncate_tool_result_chars=cfg.tape.truncate_tool_result_chars,
        truncate_tool_code_chars=cfg.tape.truncate_tool_code_chars,
    )
    md_full = render_tape_markdown(
        events,
        title=(title or f"Session — {session_dir.name}") + " (full)",
        meta=meta,
        artifacts_dir=artifacts if any(e.get("kind") == "tool_result" for e in events) else None,
        source_note=source_note,
        mode="full",
    )
    out_trunc = session_dir / "session.tape.md"
    out_full = session_dir / "session.full.tape.md"
    out_trunc.write_text(md_trunc, encoding="utf-8")
    out_full.write_text(md_full, encoding="utf-8")
    return out_trunc


def export_json_to_tape(
    export_path: Path,
    output: Path | None = None,
) -> Path:
    """Convert Claude export JSON directly to session.tape.md + session.full.tape.md."""
    data = json.loads(export_path.read_text(encoding="utf-8"))
    events = export_json_to_events(data)
    out = output or export_path.with_suffix(".tape.md")
    out_full = out.with_name(out.stem.replace(".tape", "") + ".full.tape.md") if out.name.endswith(".tape.md") else out.with_suffix(".full.tape.md")
    meta = data.get("meta") or {}
    note = (
        "Source: Claude for Excel export JSON. "
        "Snipped blocks appear as callouts; run `excel-archive watch` during sessions "
        "for pre-snip tool payloads."
    )
    md_trunc = render_tape_markdown(
        events,
        title=f"Session tape — {export_path.stem}",
        meta=meta,
        artifacts_dir=out.parent / "artifacts" if output else export_path.parent / "artifacts",
        source_note=note,
        mode="truncated",
    )
    md_full = render_tape_markdown(
        events,
        title=f"Session tape — {export_path.stem} (full)",
        meta=meta,
        artifacts_dir=out.parent / "artifacts" if output else export_path.parent / "artifacts",
        source_note=note,
        mode="full",
    )
    out.write_text(md_trunc, encoding="utf-8")
    out_full.write_text(md_full, encoding="utf-8")
    return out
