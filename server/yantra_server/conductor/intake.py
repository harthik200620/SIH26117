"""Intake (SPEC §8.2): cheap grounding + a constrained GoalSpec, questions in ask mode."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from yantra_server.gateway.engines.base import ChatMessage, Decoding
from yantra_server.gateway.service import ModelRequest
from yantra_server.observe.tracing import span

from .types import DeliverableSpec, GoalSpec

if TYPE_CHECKING:
    from yantra_server.state import AppState

TREE_MAX_ENTRIES = 120
TREE_MAX_DEPTH = 3

INTAKE_SYSTEM = """You turn a user's goal into a GoalSpec for an industrial agent workbench.
Be concrete and conservative:
- objective: one sentence, the user's words tightened, nothing added.
- deliverables: only what the goal asks for or clearly implies (a report → docx; a
  register/table → xlsx; code → code). Name them like files.
- constraints/success_criteria: verbatim requirements from the goal plus obvious ones
  (citations required for claims about documents).
- context_refs: files/folders/collections the goal mentions that exist in the grounding.
- open_questions: ONLY blockers a wrong guess would waste the whole run on (ambiguous
  unit/asset, missing scope decision). Routine choices are assumptions, not questions.
- assumptions: the defaults you chose for anything ambiguous but low-risk."""


def workspace_tree(workspace: Path, max_entries: int = TREE_MAX_ENTRIES) -> str:
    """Depth-capped, size-capped tree for grounding."""
    if not workspace.is_dir():
        return "(workspace is empty)"
    lines: list[str] = []
    count = 0
    base = len(workspace.parts)
    for path in sorted(workspace.rglob("*")):
        depth = len(path.parts) - base
        if depth > TREE_MAX_DEPTH:
            continue
        if any(
            p in {".git", "__pycache__", "node_modules", ".venv", ".yantra"} for p in path.parts
        ):
            continue
        indent = "  " * (depth - 1)
        lines.append(f"{indent}{path.name}{'/' if path.is_dir() else ''}")
        count += 1
        if count >= max_entries:
            lines.append("… (truncated)")
            break
    return "\n".join(lines) or "(workspace is empty)"


def project_memory(workspace: Path) -> str:
    """YANTRA.md from the workspace, its parents (up to home), and ~/.yantra (SPEC §13)."""
    texts: list[str] = []
    seen: set[Path] = set()
    home = Path.home()
    candidates = [workspace, *workspace.parents]
    scoped = [p for p in candidates if p == home or home in p.parents or home == p]
    for directory in [*(scoped or candidates[:1]), home / ".yantra"]:
        candidate = directory / "YANTRA.md"
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        texts.append(f"# From {candidate}\n{candidate.read_text(encoding='utf-8')[:4000]}")
    return "\n\n".join(texts)


async def build_goal_spec(
    state: AppState,
    goal_text: str,
    workspace: Path,
    collections: list[str],
    attachments: list[str],
) -> GoalSpec:
    if state.config.execution.compact_planning:
        # Intake must not reinterpret unrelated filenames as output locations. The
        # executor retrieves evidence separately; preserve the user's objective here.
        match = re.fullmatch(
            r"(?:write|create)\s+[\"'`]?([\w./-]+\.(?:txt|md|csv|json|py))[\"'`]?\s+containing\s+(?:exactly\s+)?(.+)",
            goal_text.strip(),
            re.I | re.S,
        )
        deliverables = [DeliverableSpec(name=match[1], type="text")] if match else []
        return GoalSpec(objective=goal_text, deliverables=deliverables)
    with span("intake", kind="intake", goal=goal_text[:300]) as sp:
        grounding_parts = [f"Workspace tree:\n{workspace_tree(workspace)}"]
        memory = project_memory(workspace)
        if memory:
            grounding_parts.append(f"Project memory:\n{memory}")
        if attachments:
            grounding_parts.append("Attached files:\n" + "\n".join(attachments))
        if collections:
            grounding_parts.append(f"Active knowledge collections: {', '.join(collections)}")
            hits = await _retrieval_titles(state, goal_text, collections)
            if hits:
                grounding_parts.append("Top matching documents:\n" + "\n".join(hits))
        skills = _matching_skills(state, goal_text)
        if skills:
            grounding_parts.append("Relevant skills (proven plans for similar work):\n" + skills)

        result = await state.gateway.chat(
            ModelRequest(
                role="planner",
                messages=[
                    ChatMessage(role="system", content=INTAKE_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=f"Goal:\n{goal_text}\n\nGrounding:\n"
                        + "\n\n".join(grounding_parts),
                    ),
                ],
                schema_model=GoalSpec,
                decoding=Decoding(temperature=0.0, max_tokens=2000),
            )
        )
        spec = result.parsed
        assert isinstance(spec, GoalSpec)
        # Tightening wording must never erase literal content or constraints the user gave.
        spec.objective = goal_text
        sp.set("deliverables", [d.name for d in spec.deliverables])
        sp.set("open_questions", spec.open_questions)
        return spec


async def _retrieval_titles(state: AppState, goal: str, collections: list[str]) -> list[str]:
    if state.knowledge is None:
        return []
    try:
        hits = await state.knowledge.search(goal, collections=collections, k=5)
        return [f"- {h.title} (p.{h.page}) — {h.snippet[:100]}" for h in hits]
    except Exception:
        return []


def _matching_skills(state: AppState, goal: str) -> str:
    if state.memory is None:
        return ""
    try:
        return str(state.memory.matching_skills_text(goal, top=2))
    except Exception:
        return ""
