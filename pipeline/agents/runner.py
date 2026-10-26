"""coding agent를 headless로 한 번 실행한다.

파이프라인과 같은 방식이다. 스케줄러(여기서는 실험 스크립트)는 모델 API를 부르지 않는다.
작업 디렉터리에 과제 파일과 도구를 두고 coding agent 프로세스를 띄운 뒤,
종료되면 정해진 경로의 결과 파일을 회수해 검사한다.

런타임마다 headless 실행 방법이 다르므로 명령은 설정 파일의 템플릿으로 받는다.
템플릿 자리표시자: {prompt} {task} {prompt_file} {workdir} {model}, 과제별 추가 값(예: X2의 {image})
  {prompt}는 "TASK.md를 읽어라"라는 짧은 지시이고, {task}는 TASK.md 본문 전체이다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

KICKOFF = "작업 디렉터리의 TASK.md를 읽고 지시대로 수행하라. 끝나면 종료하라."


@dataclass
class AgentConfig:
    name: str                   # 결과 표에 쓸 이름
    family: str                 # 모델 계열 (Author/Verifier 계열 중복 판단용)
    runtime: str                # pi, codex, claude-code 등
    command: list[str]          # 실행 명령 템플릿
    model: str = ""
    env: dict[str, str] = field(default_factory=dict)
    vision: bool = False        # 이미지 파일을 읽을 수 있는 구성인지
    timeout_s: float = 1800


def load_agents(path: str | Path) -> list[AgentConfig]:
    """env 값의 {config_dir}은 설정 파일이 있는 디렉터리로 바꾼다."""
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    agents = []
    for a in raw["agents"]:
        # "_"로 시작하는 키는 설명용 메모로 보고 무시한다.
        a = {k: v for k, v in a.items() if not k.startswith("_")}
        a["env"] = {k: v.replace("{config_dir}", str(path.parent)) for k, v in a.get("env", {}).items()}
        agents.append(AgentConfig(**a))
    return agents


@dataclass
class AgentRun:
    returncode: int | None
    timed_out: bool
    duration_s: float
    stdout_tail: str
    stderr_tail: str

    @property
    def exit_ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


class IsolationUnavailable(RuntimeError):
    pass


def sandbox_profile(deny: list[Path], allow: list[Path]) -> str:
    """macOS sandbox-exec 프로필: deny 아래는 읽기·쓰기 금지, 그 안의 allow만 허용 (뒤의 규칙이 이김)."""
    q = lambda p: str(Path(p).resolve()).replace('"', '\\"')  # noqa: E731
    rules = ["(version 1)", "(allow default)"]
    rules += [f'(deny file-read* file-write* (subpath "{q(p)}"))' for p in deny]
    rules += [f'(allow file-read* file-write* (subpath "{q(p)}"))' for p in allow]
    return "".join(rules)


def isolate(cmd: list[str], deny: list[Path], allow: list[Path]) -> list[str]:
    """에이전트 프로세스가 deny(저장소, 다른 후보의 작업 디렉터리 등)를 보지 못하게 감싼다.
    격리는 지침이 아니라 파일 시스템으로 강제한다. 수단이 없으면 실행하지 않는다."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return ["sandbox-exec", "-p", sandbox_profile(deny, allow), *cmd]
    raise IsolationUnavailable("이 OS에서 쓸 격리 수단이 없음 (macOS sandbox-exec. Linux는 bubblewrap 등으로 추가 예정)")


def run_agent(cfg: AgentConfig, workdir: Path, task: str, extra: dict[str, str] | None = None,
              sandbox: tuple[list[Path], list[Path]] | None = None) -> AgentRun:
    """TASK.md를 쓰고 agent를 workdir에서 실행한다. sandbox=(deny, allow)를 주면 파일 시스템 격리 안에서 실행한다."""
    prompt_file = workdir / "TASK.md"
    prompt_file.write_text(task, encoding="utf-8")
    subs = {"prompt": KICKOFF, "task": task, "prompt_file": str(prompt_file), "workdir": str(workdir), "model": cfg.model,
            **(extra or {})}
    # str.format을 쓰면 과제 본문의 중괄호가 깨지므로 자리표시자만 직접 바꾼다.
    cmd = []
    for part in cfg.command:
        for k, v in subs.items():
            part = part.replace("{" + k + "}", v)
        cmd.append(part)
    env = {**os.environ, **cfg.env}
    if sandbox is not None:
        cmd = isolate(cmd, *sandbox)

    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True,
                           timeout=cfg.timeout_s, stdin=subprocess.DEVNULL)
        rc, timed_out, out, err = p.returncode, False, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, timed_out = None, True
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    except FileNotFoundError as e:
        rc, timed_out, out, err = 127, False, "", str(e)
    return AgentRun(rc, timed_out, time.monotonic() - t0, out[-4000:], err[-4000:])


# 에이전트가 시뮬레이터를 돌리면 생기는 빌드 산출물. 결과로 가져가지 않으므로 쓰기 범위 검사에서 뺀다
# (RTLLM adder_pipe_64bit: Repair가 build/sim/Vtop.cpp를 만들어 쓰기 범위 위반으로 실패)
BUILD_OUTPUTS = ("obj_dir/", "sim_build/", "build/", "results.xml")


def snapshot(root: Path, exclude: tuple[str, ...]) -> dict[str, str]:
    """root 아래 파일의 해시. exclude로 시작하는 상대 경로, 빌드 산출물, 숨김 최상위 항목은 뺀다."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if (not p.is_file() or rel.startswith(exclude) or rel.startswith(BUILD_OUTPUTS)
                or rel.split("/")[0].startswith(".") or "__pycache__" in rel):
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def scope_violations(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """쓰기 범위 밖에서 생기거나 바뀌거나 지워진 파일."""
    changed = [f"수정: {k}" for k in before if k in after and before[k] != after[k]]
    removed = [f"삭제: {k}" for k in before if k not in after]
    added = [f"추가: {k}" for k in after if k not in before]
    return changed + removed + added
