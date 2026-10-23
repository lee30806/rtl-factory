"""Classifier: 실패를 유형으로 나누고 처리할 곳을 정한다.

입력은 세 종류다. 모두 결정론적으로 분류한다.
- Validator 결과(`Issue`)
- Verify 결과(`Finding`). 파이프라인 밖 툴(Superlint, DCLint 등)의 리포트도 같은 형식으로 바꿔 넣는다.
- patch 툴이 거부한 patch

lint 결과는 Emitter 소스맵으로 IR 노드에 대응시킨다. LLM이 쓴 내용의 줄(`fragment` 항목)이면 Repair로,
골격 줄이면 생성기 버그로 보고 사람에게 보낸다.

실패 유형 11종 중 아래 넷은 툴 결과 하나로 판별할 수 없고, 스케줄러가 루프 이력이나 에이전트 판정을 보고
붙인다 (P4): 기댓값 오류 의심, 자식 모듈 원인 의심, Verifier 간 불일치, 후보 간 불일치.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from pipeline.patch.apply import writable

# 유형: (설명, 처리할 곳)
TYPES = {
    "schema":                 ("스키마 위반", "retry"),           # 같은 에이전트 재시도
    "ir_semantic":            ("IR 의미 오류", "repair"),
    "lint_fragment":          ("lint 에러, 조각 내부", "repair"),
    "lint_skeleton":          ("lint 에러, 골격", "human"),       # 생성기 버그
    "sim_mismatch":           ("sim 불일치", "repair"),           # N회 실패하면 스케줄러가 Arbiter로 올림
    "tb_error":               ("TB 실행 오류", "verifier"),
    "expectation_suspect":    ("기댓값 오류 의심", "arbiter"),
    "child_suspect":          ("자식 모듈 원인 의심", "human"),
    "interface_change":       ("인터페이스 변경 필요", "architect"),
    "verifier_disagreement":  ("Verifier 간 불일치", "arbiter"),
    "candidate_disagreement": ("후보 간 불일치", "verifier"),
}
_L1 = {"module", "role", "params", "domains", "power_domains", "ports", "instances"}
_SECTIONS = _L1 | {"types", "signals", "blocks", "connections", "functions", "assertions"}
_USAGE_RULES = {"UNUSEDSIGNAL", "UNUSED", "UNDRIVEN", "UNUSEDPARAM"}


@dataclass(frozen=True)
class Classified:
    type: str
    route: str
    module: str | None
    node: str | None      # 대상 IR 노드. Repair는 이 노드만 고치면 된다
    message: str
    source: dict          # 원래 결과 (Issue, Finding, 거부된 patch)

    @classmethod
    def of(cls, type_: str, module, node, message, source) -> "Classified":
        src = asdict(source) if is_dataclass(source) else dict(source)
        return cls(type_, TYPES[type_][1], module, node, message, src)


def classify_issues(module: str, issues) -> list[Classified]:
    """Validator 결과. 경고는 실패가 아니므로 건너뛴다."""
    out = []
    for i in issues:
        if i.severity != "error":
            continue
        t = "schema" if i.stage == "V1" else "interface_change" if i.stage == "V11" else "ir_semantic"
        out.append(Classified.of(t, module, i.node, f"[{i.stage}] {i.message}", i))
    return out


def locate(sourcemap: list[dict], line: int) -> tuple[str | None, bool]:
    """방출된 줄 → (가장 좁은 IR 노드, LLM이 쓴 내용인지)."""
    hits = [e for e in sourcemap if e["start"] <= line <= e["end"]]
    frags = [e for e in hits if e.get("fragment")]
    pick = frags or hits
    if not pick:
        return None, False
    best = min(pick, key=lambda e: e["end"] - e["start"])
    return best["node"], bool(frags)


def py_line(sourcemap: list[dict], line: int) -> int | None:
    """방출된 줄 → Python 조각 안의 줄 (`lang: py` 모듈의 조각일 때)."""
    hits = [e for e in sourcemap if "py_line" in e and e["start"] <= line <= e["end"]]
    return min(hits, key=lambda e: e["end"] - e["start"])["py_line"] if hits else None


def classify_findings(findings, sourcemaps: dict[str, list[dict]], roles: dict[str, str]) -> list[Classified]:
    """Verify 결과와 외부 리포트. `sourcemaps`와 `roles`는 모듈 이름이 키다 (방출 파일은 `<module>.sv`)."""
    out = []
    for f in findings:
        if f.severity not in ("error", "warning"):
            continue
        if f.tool == "sva":
            # IR의 SVA 실패: assert는 RTL이 설계 의도를 어김 (Repair), assume은 테스트 입력이 가정을 어김 (Verifier)
            t = "tb_error" if f.rule == "SVA_ASSUME" else "sim_mismatch"
            out.append(Classified.of(t, Path(f.file).stem if f.file else None, f.message.split(" ", 1)[0],
                                     f"{f.test}: [SVA] {f.message}" if f.test else f"[SVA] {f.message}", f))
            continue
        if f.tool == "cocotb":
            # T1_INIT: 초기값에 따라 결과가 달라짐. 리셋이 빠진 레지스터 같은 RTL 문제다.
            t = "sim_mismatch" if f.rule in ("AssertionError", "T1_INIT") else "tb_error"
            out.append(Classified.of(t, None, None, f"{f.test}: {f.message}" if f.test else f.message, f))
            continue
        module = Path(f.file).stem if f.file else None
        if module not in sourcemaps or f.line is None:
            # 방출하지 않은 파일(표준 셀, 사람이 쓴 코드)이나 위치가 없는 결과
            out.append(Classified.of("lint_skeleton", module if module in sourcemaps else None, None,
                                     f"[{f.tool} {f.rule}] {f.message}", f))
            continue
        node, frag = locate(sourcemaps[module], f.line)
        msg = f"[{f.tool} {f.rule}] {f.message}"
        if frag and (pl := py_line(sourcemaps[module], f.line)) is not None:
            msg += f" (Python 조각 {pl}번째 줄)"
        if not frag:
            t = "lint_skeleton"
        elif writable(roles.get(module, ""), node):
            t = "lint_fragment"
        elif node.startswith("ports/") and f.rule in _USAGE_RULES and roles.get(module) == "module":
            t = "ir_semantic"  # 포트를 읽거나 구동하는 쪽(블록, 연결)을 고치면 된다
        else:
            t = "interface_change"  # 동결된 L1 값(포트 타입, 리셋값 등)이 원인
        out.append(Classified.of(t, module, node, msg, f))
    return out


def classify_rejections(module: str, rejected: list[dict]) -> list[Classified]:
    """patch 툴이 거부한 patch. L1이나 동결 범위를 고치려 했으면 인터페이스 변경 요청으로 본다."""
    out = []
    for r in rejected:
        p = r.get("patch") or {}
        target = p.get("target") if isinstance(p, dict) else None
        section = target.split("/", 1)[0] if isinstance(target, str) else None
        # 실제 섹션인데 이 role이 쓸 수 없는 곳이면 인터페이스 변경 요청. 오타나 없는 섹션은 형식 오류로 본다.
        frozen = section in _L1 or "동결" in r["reason"] or (r["reason"].startswith("쓰기 범위 밖") and section in _SECTIONS)
        out.append(Classified.of("interface_change" if frozen else "schema", module, target, r["reason"], r))
    return out
