"""에이전트 작업 인터페이스. 스케줄러는 모든 에이전트를 같은 방식으로 다룬다.

입력 산출물, 도구 집합, 쓰기 범위를 주고 coding agent를 headless로 실행한 뒤 출력 산출물을 받는다.
- 작업 디렉터리: 입력 파일, TASK.md, `tools/`(작업 디렉터리 안에서 혼자 도는 파이프라인 사본과 `pl.py`)
- 쓰기 범위: 실행 전후 스냅숏을 비교해 범위 밖 변경이 있으면 결과를 거부한다 (런타임 훅에 기대지 않음)
- 재시도: check(작업 디렉터리)가 오류를 내면 오류를 TASK.md에 붙여 retries번까지 다시 실행한다
- 기록: `.task.json`에 실행마다의 종료 코드, 시간, 출력 끝부분, 검사 결과를 남긴다 (실행 ID별 보관, 재현용)
- 격리: 에이전트 프로세스는 저장소와 같은 실행의 다른 작업 디렉터리(다른 후보, 다른 역할)를 읽거나 쓸 수 없다.
  작업 디렉터리는 저장소 밖(RUNS, 기본 ~/.cache/rtl-factory/runs)에 둔다. 저장소 안에 두면 거부한다.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from pipeline.agents.runner import AgentConfig, run_agent, scope_violations, snapshot

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path(os.environ.get("CHIPDEPT_RUNS", Path.home() / ".cache" / "rtl-factory" / "runs"))

# 런타임이 사용량 한도를 알리는 문구. 이때는 재시도하지 않고 스케줄러가 멈췄다가 나중에 이어서 실행한다.
# 구독 접근이 막힌 경우도 같다 (2026-10-22 P11 R6: 실패로 기록되어 회차 하나를 잃음)
QUOTA_PATTERNS = ("RESOURCE_EXHAUSTED", "quota reached", "rate limit", "Rate limit", "429", "usage limit", "hit your limit",
                  "session limit", "disabled Claude subscription access")


class QuotaExceeded(RuntimeError):
    pass


RETRY = """

## 이전 시도의 검사 결과 ({n}번째 재시도)
결과가 검사를 통과하지 못했다. 아래 오류를 고쳐라.

{errors}
"""


@dataclass
class Task:
    role: str                                   # architect | author | verifier | repair | arbiter | spec_check
    name: str                                   # 실행 안에서 유일 (예: author/axis_adapter/c0)
    text: str                                   # TASK.md 본문
    inputs: dict[str, str | Path] = field(default_factory=dict)   # 작업 디렉터리 상대 경로 → 내용(str) 또는 원본 파일(Path)
    tools: bool = True                          # tools/에 파이프라인 사본과 pl.py를 둘지
    outputs: list[str] = field(default_factory=list)   # 기대하는 출력 파일 (작업 디렉터리 상대 경로)
    write_scope: list[str] = field(default_factory=lambda: ["out/"])
    isolate: bool = True                        # False는 가짜 agent를 쓰는 테스트에서만


@dataclass
class TaskResult:
    task: str
    role: str
    agent: str
    workdir: str
    ok: bool
    attempts: int
    duration_s: float
    outputs: dict[str, str]                     # 출력 상대 경로 → 절대 경로 (있는 것만)
    missing: list[str]
    violations: list[str]
    errors: list[str]                           # 마지막 검사의 오류
    runs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def install_tools(work: Path) -> None:
    """에이전트가 작업 디렉터리 안에서 쓸 도구. 파이프라인 사본이라 저장소를 보지 않고도 돈다."""
    tools = work / "tools"
    shutil.copytree(ROOT / "pipeline", tools / "pipeline", ignore=shutil.ignore_patterns("__pycache__"))
    (tools / "schema").mkdir()
    shutil.copy(ROOT / "schema" / "ir.schema.json", tools / "schema" / "ir.schema.json")
    shutil.copy(ROOT / "pipeline" / "agents" / "pl.py", tools / "pl.py")


def prepare(task: Task, work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for rel, src in task.inputs.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(src, Path):
            shutil.copytree(src, p) if src.is_dir() else shutil.copy(src, p)
        else:
            p.write_text(src, encoding="utf-8")
    for s in task.write_scope:
        if s.endswith("/"):
            (work / s).mkdir(parents=True, exist_ok=True)
    if task.tools:
        install_tools(work)


def restore(task: Task, work: Path, before: dict[str, str], exclude: tuple[str, ...]) -> None:
    """쓰기 범위 밖 변경을 되돌린다: 입력 파일은 다시 쓰고, 처음에 없던 파일은 지운다. 도구 사본(tools/)이 바뀌면 다시 설치한다."""
    now = snapshot(work, exclude)
    for rel in now:
        if rel not in before:
            (work / rel).unlink()
    for rel, src in task.inputs.items():
        p = work / rel
        if isinstance(src, Path):
            if src.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                shutil.copytree(src, p)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, p)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
    if task.tools and any(k.startswith("tools/") and before.get(k) != now.get(k) for k in set(before) | set(now)):
        shutil.rmtree(work / "tools", ignore_errors=True)
        install_tools(work)


def run_task(cfg: AgentConfig, task: Task, work: Path, check: Callable[[Path], list[str]] | None = None,
             retries: int = 0, run_root: Path | None = None) -> TaskResult:
    """작업 하나를 실행한다. check는 작업 디렉터리를 보고 오류 목록을 돌려준다 (빈 목록이면 통과).
    run_root는 같은 실행의 작업 디렉터리들이 모인 곳이다. 에이전트는 그 안에서 자기 작업 디렉터리만 볼 수 있다."""
    work = work.resolve()
    sandbox = None
    if task.isolate:
        if work.is_relative_to(ROOT.resolve()):
            raise ValueError(f"작업 디렉터리를 저장소 안에 둘 수 없음 (격리): {work}")
        sandbox = ([ROOT, (run_root or work.parent).resolve()], [work])
    prepare(task, work)
    exclude = tuple(task.write_scope) + ("TASK.md", ".task.json")
    before = snapshot(work, exclude)
    text, runs, errors, t0 = task.text, [], [], time.monotonic()
    attempts = 0
    for attempts in range(1, retries + 2):
        r = run_agent(cfg, work, text, sandbox=sandbox)
        if not r.exit_ok and any(q in r.stderr_tail + r.stdout_tail for q in QUOTA_PATTERNS):
            # 한도 문구가 있는 줄을 남긴다 (첫 줄은 런타임의 다른 안내일 수 있음)
            line = next((ln for ln in (r.stderr_tail + "\n" + r.stdout_tail).splitlines() if any(q in ln for q in QUOTA_PATTERNS)), "")
            raise QuotaExceeded(f"{cfg.name}: 사용량 한도 ({line.strip()[:200]})")
        missing = [o for o in task.outputs if not (work / o).exists()]
        errors = [f"출력 파일이 없음: {m}" for m in missing]
        if not errors and check:
            errors = check(work)
        # 입력 파일을 고치거나 쓰기 범위 밖에 파일을 만들면 되돌리고 다시 시킨다 (Repair가 out/patches.json 대신 ir.json을 직접 고침)
        if attempts <= retries and (stray := scope_violations(before, snapshot(work, exclude))):
            errors = [f"쓰기 범위 밖 변경 (되돌림): {v}. 입력 파일은 고치지 말고 {', '.join(task.write_scope)} 안의 출력 파일만 쓴다"
                      for v in stray[:10]] + errors
            restore(task, work, before, exclude)
        runs.append({"attempt": attempts, "returncode": r.returncode, "timed_out": r.timed_out,
                     "duration_s": round(r.duration_s, 1), "stdout_tail": r.stdout_tail[-1500:],
                     "stderr_tail": r.stderr_tail[-1500:], "errors": errors[:20]})
        if not errors:
            break
        text = task.text + RETRY.format(n=attempts, errors="\n".join(f"- {e}" for e in errors[:20]))
    violations = scope_violations(before, snapshot(work, exclude))
    outs = {o: str(work / o) for o in task.outputs if (work / o).exists()}
    res = TaskResult(task.name, task.role, cfg.name, str(work), not errors and not violations, attempts,
                     round(time.monotonic() - t0, 1), outs, [o for o in task.outputs if o not in outs], violations,
                     errors + [f"쓰기 범위 밖 변경: {v}" for v in violations], runs)
    (work / ".task.json").write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return res
