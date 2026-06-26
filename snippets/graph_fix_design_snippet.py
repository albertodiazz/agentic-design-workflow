"""Drop-in idea for src/agent/graph.py.

Use this only as a guide because your actual graph.py was not included.
The important part is that `fix_design` no longer forwards the full validation
report as a vague prompt. It reads `auto_fix_plan` and produces a strict Builder
instruction.
"""

from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import HumanMessage

from agent.utils.fixer_prompt import build_fix_design_prompt


async def fix_design(state: dict[str, Any], runtime: Any) -> Dict[str, Any]:
    validation_report = state.get("validation_report")
    fix_iterations = int(state.get("fix_iterations", 0) or 0)
    max_fix_iterations = int(state.get("max_fix_iterations", 1) or 1)

    prompt = build_fix_design_prompt(
        validation_report,
        fix_iteration=fix_iterations + 1,
        max_fix_iterations=max_fix_iterations,
    )

    messages = list(state.get("messages", []))
    messages.append(HumanMessage(content=prompt))

    return {
        "messages": messages,
        "fix_iterations": fix_iterations + 1,
        # Optional debug field; add to OverallState if you want it visible.
        "last_fix_prompt": prompt,
    }
