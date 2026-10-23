"""SPEC 단계 검사 결과의 공통 형식. 검토 항목(review item)으로 모아 사람에게 보인다."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SpecIssue:
    code: str               # 예: REG_OVERLAP, SEC_MISSING, PORT_UNREFERENCED
    severity: str           # error | warning | info | question
    where: str              # "spec.md:123", "registers.csv:7", "ports/cmd" 등
    message: str
    source: str = "tool"    # tool | llm | open_question

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return f"[{self.severity} {self.code}] {self.where}: {self.message}"


def errors(issues: list[SpecIssue]) -> list[SpecIssue]:
    return [i for i in issues if i.severity == "error"]
