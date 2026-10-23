"""고정된 SPEC Markdown(10개 섹션 템플릿)을 구조로 읽는다.

    섹션: `## <번호>. <제목>`, 하위 절: `### ...`
    표: `|`로 시작하는 연속된 줄. 첫 줄이 머리, 둘째 줄이 구분선
    요구사항: `- <ID> (필수|권장|허용) <문장>` 또는 `- <ID> 미정의: <문장>`
    시나리오: `### SC-xx 정상|코너: <제목>` 아래의 표
    열린 질문: 10번 섹션의 `- Q-xx <문장>`

모든 항목은 원본 줄 번호를 가진다 (검사 결과와 검토 항목이 원본을 가리키도록).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE = {1: "목적과 범위", 2: "클럭, 리셋, 전력", 3: "인터페이스", 4: "파라미터", 5: "기능 요구사항",
            6: "타이밍과 성능", 7: "예외와 코너 케이스", 8: "예시 시나리오", 9: "구현 제약", 10: "열린 질문"}
REQ_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[A-Z]?\d+"
_SEC = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_SUB = re.compile(r"^###\s+(.+?)\s*$")
_REQ = re.compile(rf"^-\s+({REQ_ID})\s+(?:\((필수|권장|허용)\)\s+|(미정의):\s*)?(.*)$")
_Q = re.compile(r"^-\s+(Q-\d+)\s+(.*)$")
_SC = re.compile(r"^(SC-\d+)\s+(정상|코너):\s*(.*)$")
_REF = re.compile(rf"(?<![A-Za-z0-9_-])({REQ_ID})(?![A-Za-z0-9_])")  # 한글 조사가 바로 붙어도 찾도록


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    line: int                      # 머리 줄 번호 (1부터)
    row_lines: list[int] = field(default_factory=list)

    def dicts(self) -> list[dict[str, str]]:
        return [dict(zip(self.header, r)) for r in self.rows]


@dataclass
class Requirement:
    id: str
    level: str | None              # 필수 | 권장 | 허용 | 미정의 | None (표시 없음)
    text: str
    section: int
    line: int
    refs: list[str] = field(default_factory=list)   # 문장 안에서 인용한 다른 요구사항 ID


@dataclass
class Section:
    num: int
    title: str
    line: int
    lines: list[tuple[int, str]] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)


@dataclass
class Spec:
    path: Path | None
    title: str
    sections: dict[int, Section]
    requirements: list[Requirement]
    scenarios: list[dict]
    questions: list[dict]

    @property
    def module(self) -> str:
        return self.title.split()[0] if self.title else ""

    def requirement(self, rid: str) -> Requirement | None:
        return next((r for r in self.requirements if r.id == rid), None)


def clean(cell: str) -> str:
    """표 칸의 Markdown 장식을 걷어 낸다 (굵게, 코드, 이스케이프)."""
    c = cell.strip().replace("\\~", "~").replace("\\|", "|")
    c = re.sub(r"\*\*(.+?)\*\*", r"\1", c)
    return c.strip()


def _split_row(line: str) -> list[str]:
    cells, cur, esc, code = [], "", False, False
    for ch in line.strip().strip("|"):
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            cur += ch
            esc = True
        elif ch == "`":
            code = not code
            cur += ch
        elif ch == "|" and not code:
            cells.append(cur)
            cur = ""
        else:
            cur += ch
    cells.append(cur)
    return [c.strip() for c in cells]


def parse(text: str, path: Path | None = None) -> Spec:
    lines = text.splitlines()
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), "")
    sections: dict[int, Section] = {}
    cur: Section | None = None
    sub: str | None = None
    reqs, scenarios, questions = [], [], []
    i = 0
    while i < len(lines):
        ln, no = lines[i], i + 1
        m = _SEC.match(ln)
        if m:
            cur = Section(int(m.group(1)), m.group(2), no)
            sections[cur.num] = cur
            sub = None
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        m = _SUB.match(ln)
        if m:
            sub = m.group(1)
            sc = _SC.match(sub)
            if sc and cur.num == 8:
                scenarios.append({"id": sc.group(1), "kind": sc.group(2), "title": sc.group(3), "line": no, "table": None})
        if ln.lstrip().startswith("|"):
            start = i
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append((i + 1, lines[i]))
                i += 1
            if len(block) >= 2 and set(block[1][1].replace("|", "").strip()) <= set("-: "):
                t = Table([clean(c) for c in _split_row(block[0][1])],
                          [[clean(c) for c in _split_row(b)] for _, b in block[2:]], start + 1, [n for n, _ in block[2:]])
                cur.tables.append(t)
                if scenarios and cur.num == 8 and scenarios[-1]["table"] is None:
                    scenarios[-1]["table"] = t
            continue
        cur.lines.append((no, ln))
        m = _REQ.match(ln)
        if m and cur.num in (5, 6, 7, 9):
            rid, level, undef, body = m.groups()
            reqs.append(Requirement(rid, level or undef, body.strip(), cur.num, no,
                                    [r for r in _REF.findall(body) if r != rid]))
        m = _Q.match(ln)
        if m and cur.num == 10:
            questions.append({"id": m.group(1), "text": m.group(2).strip(), "line": no})
        i += 1
    return Spec(path, title, sections, reqs, scenarios, questions)


def load(path: str | Path) -> Spec:
    path = Path(path)
    return parse(path.read_text(encoding="utf-8"), path)
