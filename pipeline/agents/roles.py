"""역할별 설정 (config/roles.json): 역할 → coding agent, 후보 수, 상한, Author 후보의 조각 언어.

Author 후보 c의 조각 언어는 `roles.author.langs[c % len(langs)]`이다 (sv 또는 py, 기본 ["sv"]).

Author와 Verifier의 모델 계열이 같으면 실행을 막지 않고 경고한다. 경고는 실행 기록과 최종 승인의 검토 항목에 남긴다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pipeline.agents.runner import AgentConfig, load_agents

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Roles:
    agents: dict[str, AgentConfig]       # 역할 → 설정
    counts: dict[str, int]
    limits: dict[str, int]
    author_langs: tuple[str, ...] = ("sv",)

    def lang_of(self, c: int) -> str:
        """Author 후보 c의 조각 언어."""
        return self.author_langs[c % len(self.author_langs)]

    def warnings(self) -> list[str]:
        a, v = self.agents.get("author"), self.agents.get("verifier")
        if a and v and a.family == v.family:
            return [f"Author({a.model})와 Verifier({v.model})가 같은 계열({a.family})임. 공통 오독을 줄이는 효과가 약하므로 "
                    "참조 모델을 사람이 검토할 때 이 점을 봄"]
        return []


def load(path: str | Path = ROOT / "config" / "roles.json") -> Roles:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    ap = Path(raw["agents"])
    agents = {a.name: a for a in load_agents(ap if ap.is_absolute() else ROOT / ap)}
    roles = {r: agents[c["agent"]] for r, c in raw["roles"].items()}
    return Roles(roles, {r: c.get("count", 1) for r, c in raw["roles"].items()}, raw.get("limits", {}),
                 tuple(raw["roles"].get("author", {}).get("langs") or ("sv",)))
