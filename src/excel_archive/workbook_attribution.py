"""Workbook hints for multi-agent Claude-for-Excel sessions (EF/GP peers)."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PEER_AGENT_LINE_RE = re.compile(
    r'-\s*excel\s*\(agent_id:\s*"([^"]+)"\)\s*:\s*(.+?)\s*$',
    re.MULTILINE,
)
CONDUCTOR_AGENT_RE = re.compile(r"(excel-[a-f0-9]+)")

EF_SHEET_CUES = frozenset(
    {
        "shop assumptions",
        "valuation",
        "assumptions",
        "is",
        "bs",
        "cf",
    }
)
GP_SHEET_CUES = frozenset(
    {
        "production",
        "backlog",
        "pipeline",
        "pp&e",
        "taxes",
    }
)


@dataclass
class AgentRegistry:
    """agent_id → workbook filename from `<connected_peers>` blocks."""

    agents: dict[str, str] = field(default_factory=dict)

    def register(self, agent_id: str, workbook: str) -> None:
        self.agents[agent_id] = workbook.strip()

    def workbook_for(self, agent_id: str) -> str | None:
        return self.agents.get(agent_id)

    def family_for_agent(self, agent_id: str) -> str | None:
        wb = self.workbook_for(agent_id)
        return workbook_family(wb) if wb else None


@dataclass(frozen=True)
class EventAttribution:
    workbook_hint: str
    confidence: float
    reasons: list[str]
    lane: str
    target_agent_id: str | None = None
    target_workbook: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "workbook_hint": self.workbook_hint,
            "confidence": round(self.confidence, 3),
            "lane": self.lane,
            "attribution_reasons": self.reasons,
        }
        if self.target_agent_id:
            out["target_agent_id"] = self.target_agent_id
        if self.target_workbook:
            out["target_workbook"] = self.target_workbook
        return out


def parse_connected_peers(text: str) -> dict[str, str]:
    """Parse `<connected_peers>` body into agent_id → workbook filename."""
    agents: dict[str, str] = {}
    for m in PEER_AGENT_LINE_RE.finditer(text):
        agents[m.group(1)] = m.group(2).strip()
    return agents


def workbook_family(name: str | None) -> str | None:
    """Return 'ef', 'gp', or None from a workbook filename or label."""
    if not name:
        return None
    lower = name.lower()
    if "ef shop" in lower or lower.startswith("ef "):
        return "ef"
    if "gp model" in lower or "gp " in lower or "gp-" in lower:
        return "gp"
    if "ef" in lower and "shop" in lower:
        return "ef"
    return None


def build_agent_registry(events: list[dict[str, Any]]) -> AgentRegistry:
    registry = AgentRegistry()
    for ev in events:
        if ev.get("kind") != "message":
            continue
        text = ev.get("text") or ""
        if "<connected_peers>" not in text:
            continue
        for agent_id, wb in parse_connected_peers(text).items():
            registry.register(agent_id, wb)
    return registry


def _text_cues(text: str) -> set[str]:
    families: set[str] = set()
    lower = text.lower()
    if "ef shop" in lower or "ef shop model" in lower:
        families.add("ef")
    if "gp model" in lower or "gp template" in lower or "gp production" in lower:
        families.add("gp")
    if re.search(r"\bef\b", lower) and "shop" in lower:
        families.add("ef")
    return families


def _sheet_cue(sheet_name: str | None) -> str | None:
    if not sheet_name:
        return None
    s = sheet_name.strip().lower()
    if s in EF_SHEET_CUES or "shop" in s:
        return "ef"
    if s in GP_SHEET_CUES:
        return "gp"
    return None


def attribute_event(
    ev: dict[str, Any],
    *,
    registry: AgentRegistry,
    local_workbook: str | None,
) -> EventAttribution:
    """Assign workbook_hint + lane for one journal event."""
    kind = ev.get("kind")
    local_family = workbook_family(local_workbook) or workbook_family(ev.get("workbook_file_name"))

    if kind == "message":
        text = ev.get("text") or ""
        mclass = ev.get("message_class") or ""
        if mclass == "peers" or text.strip().startswith("<connected_peers>"):
            return EventAttribution(
                workbook_hint="peer",
                confidence=0.95,
                reasons=["connected_peers block"],
                lane="peer",
            )
        if mclass == "conductor" or text.strip().startswith("<conductor_context>"):
            agent_ids = CONDUCTOR_AGENT_RE.findall(text)
            target = agent_ids[0] if agent_ids else None
            wb = registry.workbook_for(target) if target else None
            fam = workbook_family(wb) or "peer"
            return EventAttribution(
                workbook_hint=fam,
                confidence=0.9,
                reasons=["conductor_context", f"agent={target}"] if target else ["conductor_context"],
                lane="peer",
                target_agent_id=target,
                target_workbook=wb,
            )
        if mclass == "workbook_state":
            fn = (ev.get("initial_state") or {}).get("fileName") or local_workbook
            fam = workbook_family(str(fn)) or "local"
            return EventAttribution(
                workbook_hint=fam,
                confidence=0.95,
                reasons=[f"initial_state.fileName={fn}"],
                lane="local",
            )
        cues = _text_cues(text)
        if len(cues) == 1:
            fam = next(iter(cues))
            return EventAttribution(
                workbook_hint=fam,
                confidence=0.75,
                reasons=["text cue"],
                lane="local" if fam == local_family else "peer",
            )
        if len(cues) > 1:
            return EventAttribution(
                workbook_hint="mixed",
                confidence=0.6,
                reasons=["multiple workbook names in text"],
                lane="local",
            )
        if local_family:
            return EventAttribution(
                workbook_hint=local_family,
                confidence=0.4,
                reasons=["default to session local workbook"],
                lane="local",
            )
        return EventAttribution(
            workbook_hint="unknown",
            confidence=0.2,
            reasons=["message without workbook cue"],
            lane="unattributed",
        )

    if kind == "tool_use":
        name = ev.get("name") or ""
        inp = ev.get("input") or {}
        if name == "send_message":
            agent_id = inp.get("agent_id") if isinstance(inp.get("agent_id"), str) else None
            wb = registry.workbook_for(agent_id) if agent_id else None
            fam = workbook_family(wb) or registry.family_for_agent(agent_id or "") or "peer"
            return EventAttribution(
                workbook_hint=fam,
                confidence=0.95,
                reasons=["send_message", f"target={agent_id}"],
                lane="peer",
                target_agent_id=agent_id,
                target_workbook=wb,
            )
        sheet = inp.get("sheetName") if isinstance(inp, dict) else None
        sheet_fam = _sheet_cue(str(sheet) if sheet else None)
        text_blob = json.dumps(inp, ensure_ascii=False) if inp else ""
        text_cues = _text_cues(text_blob)
        families = set(text_cues)
        if sheet_fam:
            families.add(sheet_fam)
        if len(families) == 1:
            fam = next(iter(families))
            return EventAttribution(
                workbook_hint=fam,
                confidence=0.55 if sheet_fam else 0.45,
                reasons=[f"sheetName={sheet}"] if sheet_fam else ["tool input text cue"],
                lane="local" if fam == local_family else "unattributed",
            )
        if len(families) > 1:
            return EventAttribution(
                workbook_hint="mixed",
                confidence=0.5,
                reasons=["conflicting sheet/text cues"],
                lane="unattributed",
            )
        if local_family:
            return EventAttribution(
                workbook_hint=local_family,
                confidence=0.35,
                reasons=["cell tool; assumed active workbook focus"],
                lane="local",
            )
        return EventAttribution(
            workbook_hint="unknown",
            confidence=0.15,
            reasons=[f"{name} without sheet/workbook cue"],
            lane="unattributed",
        )

    if kind == "tool_result":
        if local_family:
            return EventAttribution(
                workbook_hint=local_family,
                confidence=0.3,
                reasons=["tool_result inherits weak local context"],
                lane="local",
            )
        return EventAttribution(
            workbook_hint="unknown",
            confidence=0.1,
            reasons=["tool_result"],
            lane="unattributed",
        )

    return EventAttribution(
        workbook_hint="unknown",
        confidence=0.0,
        reasons=[f"unhandled kind={kind}"],
        lane="unattributed",
    )


def annotate_events(
    events: list[dict[str, Any]],
    *,
    local_workbook: str | None = None,
) -> list[dict[str, Any]]:
    """Return events with workbook_hint, lane, and attribution_reasons set."""
    registry = build_agent_registry(events)
    out: list[dict[str, Any]] = []
    for ev in events:
        attr = attribute_event(ev, registry=registry, local_workbook=local_workbook)
        merged = {**ev, **attr.as_dict()}
        out.append(merged)
    return out


@dataclass
class SessionAnalysis:
    event_count: int
    hint_counts: dict[str, int]
    lane_counts: dict[str, int]
    registry: AgentRegistry
    send_message_timeline: list[dict[str, Any]]
    sample_events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "workbook_hint_counts": self.hint_counts,
            "lane_counts": self.lane_counts,
            "peer_registry": self.registry.agents,
            "send_message_timeline": self.send_message_timeline,
            "sample_timeline": self.sample_events,
        }


def load_events_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def analyze_session_events(
    events: list[dict[str, Any]],
    *,
    local_workbook: str | None = None,
    sample_limit: int = 12,
) -> SessionAnalysis:
    """Summarize attribution lanes and peer graph for a session."""
    annotated = annotate_events(events, local_workbook=local_workbook)
    hints = Counter(e.get("workbook_hint", "unknown") for e in annotated)
    lanes = Counter(e.get("lane", "unattributed") for e in annotated)
    registry = build_agent_registry(annotated)

    send_timeline: list[dict[str, Any]] = []
    for i, ev in enumerate(annotated):
        if ev.get("kind") == "tool_use" and ev.get("name") == "send_message":
            send_timeline.append(
                {
                    "index": i,
                    "agent_id": ev.get("target_agent_id"),
                    "workbook": ev.get("target_workbook"),
                    "hint": ev.get("workbook_hint"),
                }
            )

    samples: list[dict[str, Any]] = []
    for ev in annotated:
        if ev.get("workbook_hint") in ("peer", "cross_peer", "mixed") or (
            ev.get("kind") == "tool_use" and ev.get("name") == "send_message"
        ):
            samples.append(
                {
                    "kind": ev.get("kind"),
                    "name": ev.get("name"),
                    "workbook_hint": ev.get("workbook_hint"),
                    "lane": ev.get("lane"),
                    "reasons": ev.get("attribution_reasons"),
                }
            )
        if len(samples) >= sample_limit:
            break

    return SessionAnalysis(
        event_count=len(annotated),
        hint_counts=dict(hints),
        lane_counts=dict(lanes),
        registry=registry,
        send_message_timeline=send_timeline,
        sample_events=samples,
    )


def filter_events_by_hint(events: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    """Filter annotated events to one workbook_hint (ef, gp, peer, etc.)."""
    return [e for e in events if e.get("workbook_hint") == hint]
