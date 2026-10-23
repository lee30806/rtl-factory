"""Verify: E1 Verilator lint, E2 Verilator sim(cocotb). 결과는 단계와 관계없이 같은 형식으로 낸다.

결과 한 건은 (툴, 규칙, 파일, 줄, 메시지)다. E3(VC SpyGlass)은 사내 실행 템플릿과 리포트 형식이 정해지면
같은 형식으로 추가한다. 파이프라인 밖 툴의 리포트도 이 형식으로 바꿔 Classifier에 넣는다.

IR의 SVA(`assertions`)가 방출된 RTL이면 E2는 `--assert`로 빌드한다. 속성이 실패하면 RTL이 `[SVA] FAIL ...` 줄을 내고
시뮬레이션은 이어진다. 그 줄을 테스트 로그에서 읽어, 실패한 속성마다 결과 한 건(툴 `sva`, 규칙 `SVA_ASSERT`/`SVA_ASSUME`,
파일 `<모듈>.sv`, 메시지 첫 낱말이 IR 노드 `assertions/<id>`)을 그 테스트의 실패로 낸다. cocotb 테스트가 통과해도 실패다.

    python3 -m pipeline.verify.run lint --top pkt_parser out/*.sv
    python3 -m pipeline.verify.run sim  --top pkt_parser --tests tb_pkt_parser --test-dir tests/sim out/*.sv
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

LINT_ARGS = ["-Wall"]
_VLT = re.compile(r"^%(Warning|Error)(?:-([A-Z0-9_]+))?: (?:(\S+?):(\d+):(?:\d+:)?\s*)?(.*)$")


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    file: str | None
    line: int | None
    message: str
    severity: str = "error"  # error | warning
    test: str | None = None  # sim 결과만: 테스트 이름


@dataclass
class StageResult:
    stage: str               # E1 | E2 | E3
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    log: str = ""            # 툴 출력 (잘라서 보관)
    coverage: dict | None = None  # E2에서 coverage=True일 때: 종류별 {points, hit, ratio} (T5)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "ok": self.ok, "findings": [asdict(f) for f in self.findings], "log": self.log,
                "coverage": self.coverage}


def parse_verilator(text: str, tool: str = "verilator") -> list[Finding]:
    out = []
    for ln in text.splitlines():
        mo = _VLT.match(ln)
        if not mo:
            continue
        kind, rule, file, line, msg = mo.groups()
        if kind == "Error" and not rule and not file and msg.startswith("Exiting due to"):
            continue  # 요약 줄
        out.append(Finding(tool, rule or ("ERROR" if kind == "Error" else "WARNING"), file,
                           int(line) if line else None, msg.strip(), "error" if kind == "Error" else "warning"))
    return out


def lint(files: list[Path], top: str, extra_args: list[str] | None = None) -> StageResult:
    """E1. -Wall 경고도 실패로 본다 (waiver는 쓰지 않는다)."""
    cmd = ["verilator", "--lint-only", *LINT_ARGS, *(extra_args or []), "--top-module", top, *map(str, files)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    findings = parse_verilator(p.stderr + p.stdout)
    if p.returncode != 0 and not findings:
        findings = [Finding("verilator", "TOOL", None, None, (p.stderr or p.stdout).strip()[-2000:])]
    return StageResult("E1", p.returncode == 0 and not findings, findings, (p.stderr + p.stdout)[-20000:])


def parse_cocotb_results(path: Path) -> tuple[int, list[Finding]]:
    """cocotb results.xml → (테스트 수, 실패). 규칙은 예외 타입이다 (AssertionError는 기댓값 불일치)."""
    out, total = [], 0
    for tc in ET.parse(path).getroot().iter("testcase"):
        total += 1
        bad = tc.find("failure")
        if bad is None:
            bad = tc.find("error")
        if bad is None:
            continue
        props = {p.get("name"): p.get("value") for p in tc.iter("property")}
        line = props.get("line")
        out.append(Finding("cocotb", bad.get("type") or "Error", props.get("file"), int(line) if line else None,
                           (bad.get("message") or "").strip(), test=tc.get("name")))
    return total, out


REPO = Path(__file__).resolve().parents[2]
# T1: Verilator는 2-state라 X를 볼 수 없다. 초기값을 시드별로 무작위화해 결과가 달라지는지 본다.
RANDOM_INIT_BUILD = ["-x-assign", "unique", "-x-initial", "unique"]
# 커버리지 계측은 always 블록을 쪼개 실제로는 없는 조합 루프 경고(UNOPTFLAT)를 낸다. 이 경고는 계측 없는
# 빌드와 E1에서 본다 (axis_cobs_decode 이식 시험에서 확인).
COVERAGE_BUILD = ["--coverage", "-Wno-UNOPTFLAT"]


_COV = re.compile(r"^C '(.*)' (\d+)$")


def parse_coverage(paths: list[Path]) -> dict:
    """Verilator coverage.dat들을 합쳐 종류별(line, toggle, branch 등) 커버리지와 안 덮인 줄을 낸다."""
    points: dict[str, int] = {}
    for p in paths:
        for ln in Path(p).read_text(errors="replace").splitlines():
            mo = _COV.match(ln)
            if mo:
                points[mo.group(1)] = max(points.get(mo.group(1), 0), int(mo.group(2)))
    out: dict = {}
    for key, n in points.items():
        f = dict(x.split("\x02", 1) for x in key.split("\x01") if "\x02" in x)
        t = out.setdefault(f.get("t", "?"), {"points": 0, "hit": 0, "missed": []})
        t["points"] += 1
        if n:
            t["hit"] += 1
        elif f.get("t") != "toggle":
            t["missed"].append(f'{Path(f.get("f", "?")).name}:{f.get("l", "?")}')
    for t in out.values():
        t["ratio"] = round(t["hit"] / t["points"], 3) if t["points"] else None
        t["missed"] = sorted(set(t["missed"]))[:50]
    return out


SIM_TIMEOUT_S = float(os.environ.get("CHIPDEPT_SIM_TIMEOUT", "900"))
SVA_MARK = "[SVA] FAIL"   # pipeline/emitter/sv.py의 SVA_FAIL과 같음
_SVA_LINE = re.compile(r"\[SVA\] FAIL module=(\S+) id=(\S+) kind=(\S+) at (\S+)")
_RUNNING = re.compile(r"running \S+?\.(\S+) \(")


def parse_sva(log: str) -> list[Finding]:
    """테스트 로그의 SVA 실패 줄 → 결과 (테스트, 모듈, 속성)마다 한 건. 줄이 속한 테스트는 앞의 `running <테스트>` 줄이다
    (RTL은 보고 줄마다 $fflush 하므로 cocotb 로그와 순서가 맞음)."""
    test, seen, out = None, {}, []
    for ln in log.splitlines():
        if mo := _RUNNING.search(ln):
            test = mo.group(1)
        elif mo := _SVA_LINE.search(ln):
            mod, aid, kind, where = mo.groups()
            key = (test, mod, aid)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 1:
                out.append([test, mod, aid, kind, where])
    return [Finding("sva", "SVA_ASSUME" if kind == "assume" else "SVA_ASSERT", f"{mod}.sv", None,
                    f"assertions/{aid} ({mod}, {where}) {kind} 실패 {seen[(test, mod, aid)]}회", test=test)
            for test, mod, aid, kind, where in out]


def sim(files: list[Path], top: str, test_module: str, test_dir: Path, build_dir: Path,
        build_args: list[str] | None = None, seed: int | None = None, seeds: list[int] | None = None,
        harness_dir: Path | None = None, trace: Path | None = None, env: dict[str, str] | None = None,
        timescale: tuple[str, str] | None = None, coverage: bool = False, timeout_s: float = SIM_TIMEOUT_S,
        rebuild: bool = True) -> StageResult:
    """E2. cocotb + Verilator. 빌드 실패도 이 단계의 실패로 낸다.

    rebuild=False면 build_dir에 이미 만든 시뮬레이터를 다시 쓴다 (같은 RTL로 자극만 바꿔 여러 번 돌릴 때).
    시드 하나의 실행이 timeout_s초를 넘으면 시뮬레이터를 끝내고 `SIM_TIMEOUT`으로 낸다 (RTL이 멈춰 테스트가 끝나지 않을 때).

    seeds를 주면 초기값을 무작위화해 시드마다 실행하고(T1, Verilator 시드만 바꾸고 Python random 시드는 seed로 고정), 시드에 따라 실패하는 테스트가 달라지면
    그 자체를 실패(`T1_INIT`)로 낸다. harness_dir는 생성된 하니스가 있는 디렉터리, trace는 내부 신호 목록
    (`<module>_trace.json`)이다. 하니스 결과(첫 불일치, 트레이스)는 `build_dir/tb_out/<시드>/`에 남는다.
    coverage=True면 Verilator line/toggle 커버리지를 모든 시드에 걸쳐 합쳐 `coverage`에 낸다(T5).
    """
    from cocotb_tools.runner import get_runner

    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    build_log = build_dir / "build.log"
    runner = get_runner("verilator")
    # IR의 SVA가 있는 RTL만 --assert로 빌드한다 (원본 RTL의 unique case 같은 검사가 새로 켜지지 않게)
    sva = any(SVA_MARK in Path(f).read_text(errors="replace") for f in files)
    # cocotb는 빌드 환경의 PYTHONPATH를 이 프로세스의 sys.path로 둔다. 빌드 중 make가 부르는 python3가 다른 버전이면
    # 그 표준 라이브러리를 잘못 읽어 죽으므로, 이 인터프리터의 python3를 PATH 맨 앞에 둔다
    py_bin = str(Path(sys.executable).parent)
    if not os.environ.get("PATH", "").startswith(py_bin + os.pathsep):
        os.environ["PATH"] = py_bin + os.pathsep + os.environ.get("PATH", "")
    try:
        # runner는 test 전에 같은 객체에서 build를 불러야 한다. 다시 쓸 때는 always=False로 소스가 바뀌었을 때만 빌드한다
        runner.build(sources=[Path(f) for f in files], hdl_toplevel=top, build_dir=build_dir,
                     build_args=[*(build_args or []), *(["--assert"] if sva else []), *(RANDOM_INIT_BUILD if seeds else []),
                                 *(COVERAGE_BUILD if coverage else [])],
                     always=rebuild or not (build_dir / top).exists(),
                     log_file=build_log, timescale=timescale)
    except (Exception, SystemExit):
        text = build_log.read_text(errors="replace") if build_log.exists() else ""
        findings = parse_verilator(text) or [Finding("verilator", "BUILD", None, None, text.strip()[-2000:])]
        return StageResult("E2", False, findings, text[-20000:])
    paths = [str(REPO)] + ([str(Path(harness_dir).resolve())] if harness_dir else [])
    per_seed: dict[int | None, list[Finding]] = {}
    logs = []
    for s in (seeds or [seed]):
        tag = "default" if s is None else str(s)
        results, test_log = build_dir / f"results.{tag}.xml", build_dir / f"test.{tag}.log"
        results.unlink(missing_ok=True)
        env = {**(env or {}), "TB_OUT": str((build_dir / "tb_out" / tag).resolve())}
        if trace:
            env["TB_TRACE"] = str(Path(trace).resolve())
        plus = [f"+verilator+seed+{s}", "+verilator+rand+reset+2"] if seeds else []
        cov_file = build_dir / f"coverage.{tag}.dat"
        if coverage:
            cov_file.unlink(missing_ok=True)
            plus.append(f"+verilator+coverage+file+{cov_file.resolve()}")
        saved = list(sys.path)
        sys.path[:0] = paths  # runner는 PYTHONPATH를 이 프로세스의 sys.path로 정한다
        timed_out = threading.Event()
        # runner는 시뮬레이터를 하위 프로세스로 돌린다. 빌드 디렉터리의 실행 파일 경로로 그 프로세스만 찾아 끝낸다
        watchdog = threading.Timer(timeout_s, lambda: (timed_out.set(), subprocess.run(
            ["pkill", "-f", str((build_dir / top).resolve())], capture_output=True)))
        watchdog.start()
        try:
            # 자극이 시드에 따라 바뀌지 않도록 cocotb(Python random) 시드는 고정한다.
            runner.test(hdl_toplevel=top, test_module=test_module, test_dir=Path(test_dir), build_dir=build_dir,
                        results_xml=str(results), seed=(seed if seed is not None else 1) if seeds else s,
                        log_file=test_log, extra_env=env, plusargs=plus, timescale=timescale)
        except (Exception, SystemExit):
            pass  # 테스트가 실패하면 runner가 예외를 던진다. 판정은 결과 파일로 한다.
        finally:
            watchdog.cancel()
            sys.path[:] = saved
        full_log = test_log.read_text(errors="replace") if test_log.exists() else ""
        logs.append(full_log[-20000:])
        if timed_out.is_set():
            running = re.findall(r"running \S+?\.(\S+) \(", logs[-1])
            last = running[-1] if running else None
            per_seed[s] = [Finding("cocotb", "SIM_TIMEOUT", None, None,
                                   f"{timeout_s:.0f}초 안에 끝나지 않아 중단 (마지막 테스트 {last}, seed {tag})", test=last)]
            continue
        if not results.exists():
            per_seed[s] = [Finding("cocotb", "NO_RESULTS", None, None, f"결과 파일이 없음 (시뮬레이터 비정상 종료, seed {tag})")]
            continue
        total, findings = parse_cocotb_results(results)
        if total == 0:
            findings = [Finding("cocotb", "NO_TESTS", None, None, f"{test_module}에서 실행된 테스트가 없음")]
        per_seed[s] = findings + (parse_sva(full_log) if sva else [])
    findings = list(dict.fromkeys(f for fs in per_seed.values() for f in fs))
    failed_sets = {frozenset(f.test for f in fs) for fs in per_seed.values()}
    if len(failed_sets) > 1:
        detail = ", ".join(f"seed {s}: {sorted(f.test for f in fs) or '통과'}" for s, fs in per_seed.items())
        findings.insert(0, Finding("cocotb", "T1_INIT", None, None, f"초기값에 따라 결과가 달라짐 ({detail})"))
    elif seeds and trace and len(seeds) > 1 and len(json.loads(Path(trace).read_text()).get("domains", [None])) == 1:
        # 클럭이 여럿이면 초기값 무작위화가 클럭 입력의 초기값도 바꿔 도메인 사이 위상이 시드마다 달라진다.
        # 그때 출력이 언제 보이는지는 CDC 지연 때문에 정상적으로 달라지므로, 실패 목록 비교(위)만 쓴다.
        tcfg = json.loads(Path(trace).read_text())
        diff = _compare_outputs(build_dir / "tb_out", [str(s) for s in seeds], tcfg["outputs"], tcfg.get("qualifiers", {}))
        if diff:
            findings.insert(0, Finding("cocotb", "T1_INIT", None, None, f"초기값에 따라 출력이 달라짐 ({diff})", test=diff.split(":")[0]))
    cov = parse_coverage(sorted(build_dir.glob("coverage.*.dat"))) if coverage else None
    return StageResult("E2", not findings, findings, "\n".join(logs)[-20000:], cov)


def _compare_outputs(tb_out: Path, tags: list[str], outputs: list[str], qualifiers: dict[str, str] | None = None) -> str | None:
    """시드별 트레이스에서 DUT 출력값을 사이클 단위로 비교해 처음 달라진 곳을 돌려준다.
    qualifiers의 data 출력은 양쪽 모두 valid가 0이면 비교하지 않는다 (리셋 없는 data 레지스터의 초기값)."""
    qualifiers = qualifiers or {}
    base = tb_out / tags[0]
    for f in sorted(base.glob("*.trace.json")):
        ref = json.loads(f.read_text())
        for tag in tags[1:]:
            other = tb_out / tag / f.name
            if not other.exists():
                continue
            oth = {(t["domain"], t["cycle"]): t for t in json.loads(other.read_text())}
            # 도메인이 여럿이면 항목 순서가 실행마다 다를 수 있으므로 (도메인, 사이클)로 짝짓는다.
            for a in ref:
                b = oth.get((a["domain"], a["cycle"]))
                if b is None:
                    return (f'{f.name.removesuffix(".trace.json")}: 실행 길이가 다름 '
                            f'({a["domain"]} cycle {a["cycle"]}이 seed {tag}에 없음)')
                for o in outputs:
                    if o not in a["values"] and o not in b["values"]:
                        continue  # 이 도메인에서 읽지 않는 출력
                    q = qualifiers.get(o)
                    if q and not a["values"].get(q) and not b["values"].get(q):
                        continue
                    if a["values"].get(o) != b["values"].get(o):
                        return (f'{f.name.removesuffix(".trace.json")}: {a["domain"]} cycle {a["cycle"]} {o} = '
                                f'{a["values"].get(o)} (seed {tags[0]}) / {b["values"].get(o)} (seed {tag})')
            if len(ref) != len(oth):
                return (f'{f.name.removesuffix(".trace.json")}: 실행 길이가 다름 '
                        f'({len(ref)} (seed {tags[0]}) / {len(oth)} (seed {tag}))')
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E1 lint / E2 sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lint")
    a.add_argument("--top", required=True)
    a.add_argument("files", nargs="+")
    b = sub.add_parser("sim")
    b.add_argument("--top", required=True)
    b.add_argument("--tests", required=True, help="cocotb 테스트 모듈 이름")
    b.add_argument("--test-dir", required=True)
    b.add_argument("--build-dir", default="build/sim")
    b.add_argument("--seed", type=int)
    b.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    files = [Path(f) for f in args.files]
    if args.cmd == "lint":
        r = lint(files, args.top)
    else:
        r = sim(files, args.top, args.tests, Path(args.test_dir), Path(args.build_dir), seed=args.seed)
    d = r.to_dict()
    d.pop("log")
    print(json.dumps(d, ensure_ascii=False, indent=1))
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
