"""B2: ChipVerilog 로더와 채점 (BENCHMARK_PLAN B2).

입력은 pymtl에서 엔진 간 공정 비교용으로 미리 만든 CVDP 형식 JSONL(`build/cvdp_datasets/cvdp_chipverilog.jsonl`)이다.
레코드의 `input`(원문 프롬프트 + 추가 프롬프트)을 SPEC 작성 입력으로 쓰고, `harness.files`에서 헤더 include와 재사용 셀을 꺼낸다.

채점은 ChipVerilog 공식 도구(`build/datasets/ChipVerilog/tools/formal_equivalence.py verify`)로 한다: iverilog 컴파일 게이트,
자체 검사 TB가 있으면 시뮬레이션, 없으면 Yosys 등가(유도/유한 깊이). 공식 판정 `status == "pass"`만 통과다.
정답 RTL(`Des/`)은 채점에만 쓰고 에이전트에게 주지 않는다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from experiments.bench.common.modules import dedupe_modules  # noqa: F401  (시험과 pymtl 비교 스크립트가 여기서 가져온다)
from experiments.bench.common.schema import BenchItem, EvalSpec

# 이 저장소(experiments/bench/adapters)와 pymtl(src/rtl_factory/bench/adapters) 어디에 있든 위로 올라가며 데이터를 찾는다
CV_ROOT = next((d / "build" / "datasets" / "ChipVerilog" for d in Path(__file__).resolve().parents
                if (d / "build" / "datasets" / "ChipVerilog" / "tools").exists()), Path("build/datasets/ChipVerilog"))
TOOL = CV_ROOT / "tools" / "formal_equivalence.py"


def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def des_dir(original_id: str) -> Path:
    hits = [p for p in CV_ROOT.glob(f"Des/*/{original_id}") if p.is_dir()]
    if len(hits) != 1:
        raise FileNotFoundError(f"ChipVerilog Des 디렉터리를 찾지 못함: {original_id} ({hits})")
    return hits[0]


def reference_file(d: Path) -> Path:
    """공식 도구(discover_reference_file)와 같은 규칙: `<모듈>.v`, 없으면 디렉터리의 유일한 .v."""
    if (f := d / f"{d.name}.v").exists():
        return f
    vs = sorted(d.glob("*.v"))
    if len(vs) != 1:
        raise FileNotFoundError(f"정답 파일을 정하지 못함: {d}")
    return vs[0]


def raw_description(original_id: str) -> Path:
    """ChipVerilog 원본 과제 설명(`Src/**/des/**/description.txt`). 디렉터리 이름이 Des와 같다 (fpu만 원본이 fpu_double).
    ChipVerilog 논문 프로토콜(run_codex_batch_txt.sh)이 모델에게 주는 유일한 입력이다."""
    name = "fpu_double" if original_id == "fpu" else original_id
    hits = [p for p in CV_ROOT.glob("Src/**/description.txt") if p.parent.name == name]
    if len(hits) != 1:
        raise FileNotFoundError(f"ChipVerilog 원본 설명을 찾지 못함: {original_id} ({hits})")
    return hits[0]


PROMPT_SOURCES = ("cvdp", "raw")

_SECTION = re.compile(r"^\s*(Input|Output|Inout|Bidirectional)\s+ports?\s*:\s*$", re.I | re.M)
_SECTION_PORT = re.compile(r"^ {1,8}(?:(?:input|output|inout)\s+)?(?:reg\s+|wire\s+)?([A-Za-z_]\w*)\s*(\[[^\]]+\])?\s*:", re.M)
_DIRS = {"input": "input", "output": "output", "inout": "inout", "bidirectional": "inout"}


def _decl_ports(text: str, top: str) -> list[tuple[str, str | None, str]] | None:
    """원문 모듈 선언의 포트 (이름, 방향 또는 None, 폭). ANSI 선언은 앞 포트의 방향·폭을 이어받는다 (Verilog 규칙)."""
    m = re.search(r"\bmodule\s+" + re.escape(top) + r"\b\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\((.*?)\)\s*;", text, re.S)
    if not m:
        return None
    body = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S))
    body = re.sub(r"^\s*`.*$", "", body, flags=re.M)        # `ifdef 같은 지시문 줄
    out, d, w = [], None, ""
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if dm := re.match(r"(input|output|inout)\b\s*(?:reg|wire|logic)?\s*(?:signed\s*)?(\[[^\]]+\])?", part):
            d, w = dm.group(1), dm.group(2) or ""
        names = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", "", part))
        if names:
            out.append((names[-1], d, w))
    return out


def header_from_description(text: str, top: str) -> tuple[str, list[str]]:
    """원본 description.txt만으로 헤더를 만든다: 포트 순서는 모듈 선언, 방향·폭은 `Input ports:`/`Output ports:` 절이 우선.
    절에 없는 포트는 선언을 따른다. 만들 수 없으면 ("", 사유). 두 번째 값은 선언과 절이 어긋나 절을 따른 포트 목록."""
    decl = _decl_ports(text, top)
    if not decl:
        return "", ["모듈 선언 없음"]
    sec: dict[str, tuple[str, str]] = {}
    heads = list(_SECTION.finditer(text))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        chunk = text[h.end():end]
        stop = re.search(r"^\S", chunk, re.M)                  # 들여쓰지 않은 다음 문단에서 절이 끝남
        for pm in _SECTION_PORT.finditer(chunk[: stop.start()] if stop else chunk):
            sec.setdefault(pm.group(1), (_DIRS[h.group(1).lower()], pm.group(2) or ""))
    lines, fixed = [], []
    for name, d, w in decl:
        sd, sw = sec.get(name, (None, ""))
        if sd and sd != d:
            if d:                                                 # 비 ANSI 선언(방향 없음)은 절을 따르는 것이 정상이라 적지 않는다
                fixed.append(f"{name}: 선언 {d}{w} → 절 {sd}{sw}")
            d, w = sd, sw
        elif sd and sw and sw.replace(" ", "") != w.replace(" ", ""):
            fixed.append(f"{name}: 선언 폭 {w or '1비트'} → 절 {sw}")
            w = sw
        if not d:
            return "", [f"포트 {name}의 방향을 정할 수 없음"]
        lines.append(f"    {d} {w + ' ' if w else ''}{name}")
    return f"module {top} (\n" + ",\n".join(lines) + "\n);\nendmodule\n", fixed


def _header_ports(header: str, top: str) -> list[tuple[str, str | None, str]]:
    return _decl_ports(header, top) or []


def to_item(rec: dict, src_root: Path, prompt_source: str = "cvdp") -> BenchItem:
    """prompt_source: `cvdp`는 pymtl 변환본 `input`(원문 + 추가 안내), `raw`는 원본 description.txt만 (논문 비교·엔진 간 공정 비교용)."""
    h, meta = rec["harness"], rec["metadata"]
    d = Path(src_root).resolve() / rec["id"]
    for rel, text in h["files"].items():
        if rel.startswith(("rtl/", "src/")):   # 정답(ref/)과 TB(tb/)는 꺼내지 않는다
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    top = meta["top_module"]
    header = meta.get("header_sv") or rec["context"].get(f"rtl/{top}.sv", "")
    header_lessons: list[str] = []
    # pymtl 변환본 헤더가 원문 모듈 선언과 다른 문제가 있다 (i2c 3개: 포트 누락, 산문 단어를 포트로 씀). 원문으로 다시 만든다.
    # 변환본의 `define 줄은 그대로 앞에 둔다 (원문 프롬프트에 있는 매크로)
    desc = raw_description(meta["original_id"]).read_text(encoding="utf-8")
    new_hdr, fixed = header_from_description(desc, top)
    if new_hdr and [(n, d) for n, d, _ in _header_ports(header, top)] != [(n, d) for n, d, _ in _header_ports(new_hdr, top)]:
        defs = "".join(ln + "\n" for ln in header.splitlines() if ln.lstrip().startswith("`define"))
        header_lessons.append(f"[헤더] 변환본 헤더의 포트가 원문 모듈 선언과 달라 원본 description.txt로 헤더를 다시 만듦"
                              + (f" (원문 선언과 포트 설명 절이 어긋나 설명 절을 따름: {'; '.join(fixed)[:400]})" if fixed else ""))
        header = defs + new_hdr
    return BenchItem(
        benchmark="chipverilog",
        id=rec["id"],
        category=meta.get("tier", "standalone"),
        top=top,
        prompt_text=(raw_description(meta["original_id"]).read_text(encoding="utf-8") if prompt_source == "raw" else rec["input"]),
        header_sv=header,
        incdirs=[str(d / x) for x in h.get("include_dirs", [])],
        cells=[str(d / c) for c in h.get("cell_files", [])],
        reference_rtl=[str(reference_file(des_dir(meta["original_id"])))],
        eval=EvalSpec(kind="chipverilog", extra={"header_lessons": header_lessons} if header_lessons else {}),
    )


def load_chipverilog(path: Path, src_root: Path, prompt_source: str = "cvdp") -> list[BenchItem]:
    if prompt_source not in PROMPT_SOURCES:
        raise ValueError(f"prompt_source는 {PROMPT_SOURCES} 중 하나: {prompt_source}")
    return [to_item(r, src_root, prompt_source) for r in load_records(path)]


_CHECK_RE = re.compile(r"^\s*\d+\s*ps\s+(Answer is correct|Error! \S+ is incorrect)\s+([0-9a-fA-Fxz]+)", re.M)
_EXPECT_RE = re.compile(r"==\s*\d+'h([0-9a-fA-F_]+)")
_STIM_RE = re.compile(r"^\s*(\w+)\s*=\s*\d+'([bh])([0-9a-fA-F_]+)\s*;", re.M)


def tb_stimuli(tb_text: str) -> list[dict[str, str]]:
    """TB 원문의 입력 대입(`opa = 64'b...;`)을 검사 순서대로 묶는다. 가장 자주 대입되는 신호들(피연산자)만 보고
    clk, rst, enable처럼 몇 번만 대입되는 신호는 뺀다. 같은 이름이 다시 나오면 다음 검사 입력. 값은 16진수로."""
    found = _STIM_RE.findall(tb_text)
    if not found:
        return []
    counts: dict[str, int] = {}
    for name, _, _ in found:
        counts[name] = counts.get(name, 0) + 1
    top = max(counts.values())
    recs: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for name, base, val in found:
        if counts[name] != top:
            continue
        if name in cur:
            recs.append(cur)
            cur = {}
        v = val.replace("_", "")
        cur[name] = format(int(v, 2), "x") if base == "b" else v.lower()
    if cur:
        recs.append(cur)
    return recs


def diagnose_sim(sim_log: str, tb_text: str = "") -> str:
    """자체 검사 TB의 출력("Answer is correct / Error! out is incorrect <값>")에서 Repair가 쓸 한 줄 진단을 만든다.
    값만 틀렸는지, 모든 검사 시점에 같은 값이 나오는지(파이프라인 깊이·enable 처리 의심), 기대값이 몇 검사 간격 어긋나 나오는지를 본다.
    TB 원문이 있으면 `== 64'h...` 기대값 목록과 대조한다. 형식이 맞지 않으면 빈 문자열."""
    checks = _CHECK_RE.findall(sim_log)
    if not checks:
        return ""
    actual = [v.lower().replace("_", "") for _, v in checks]
    n_ok = sum(1 for k, _ in checks if k.startswith("Answer"))
    parts = [f"자체 검사 {len(checks)}개 중 {n_ok}개 일치"]
    if len(actual) > 1 and len(set(actual)) == 1:
        parts.append("모든 검사 시점에 출력이 같은 값이다: 연속 입력이 순서대로 나오지 않으므로 파이프라인 깊이(지연)나 enable 처리를 먼저 의심할 것")
    expected = [v.lower().replace("_", "") for v in _EXPECT_RE.findall(tb_text)] if tb_text else []
    stim = tb_stimuli(tb_text) if tb_text else []
    if expected and len(expected) == len(actual) and n_ok < len(actual):
        # 틀린 검사마다 입력(있으면)·기대·실제를 적는다: Repair가 특수값(비정규, 오버플로) 같은 미정의 입력을 알아보게
        bad = [i for i, (k, _) in enumerate(checks) if not k.startswith("Answer")][:5]
        parts += [f"검사 {i + 1}: " + (f"입력 {stim[i]} " if len(stim) == len(actual) else "")
                  + f"기대 {expected[i]} 실제 {actual[i]}" for i in bad]
        best_k, best_hits = 0, 0
        for k in range(-(len(actual) - 1), len(actual)):
            hits = sum(1 for i, a in enumerate(actual) if 0 <= i - k < len(expected) and a == expected[i - k])
            if hits > best_hits:
                best_k, best_hits = k, hits
        if best_k and best_hits >= 2:
            # k>0: 검사 i에 기대값 i-k가 보임 = 출력이 늦음 = 후보 지연이 정답보다 길다
            parts.append(f"기대값 순서가 {abs(best_k)} 검사 간격 {'늦게' if best_k > 0 else '빠르게'} 나온다 ({best_hits}개 일치): "
                         f"지연이 정답보다 {'길다' if best_k > 0 else '짧다'}")
    return ". ".join(parts) + "."


def _verify(text: str, name: str, root: Path, timeout: int) -> tuple[dict | None, Path, str]:
    cdir = root / "result_root" / "rtlf" / name
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{name}_t1.v").write_text(text, encoding="utf-8")
    p = subprocess.run([sys.executable, str(TOOL), "verify", str(cdir), "--result-root", str(root / "result_root"),
                        "--report-dir", str(root / "reports")], cwd=CV_ROOT, capture_output=True, text=True, timeout=timeout)
    (root / "verify.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    rf = cdir / f"{name}_t1" / "result.json"
    return (json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else None), cdir / f"{name}_t1", p.stdout + p.stderr


# 후보 결함이 아니라 채점 환경이 판정을 내리지 못한 상태. 반례·실패 검사가 없어 Repair가 고칠 근거가 없다
GRADER_DEFECT_STATUS = {
    "equivalence_error": "등가 검사 도구가 판정하지 못함 (예: 메모리 셀 $mem_v2를 SAT로 옮기지 못함)",
    "equivalence_timeout": "등가 검사 시간 초과",
    "reference_fail": "정답 RTL이 채점 도구에서 컴파일되지 않음",
}


def grader_defect(r: dict) -> str:
    """공식 판정이 채점 환경 결함이면 그 이유, 후보를 고쳐야 하는 실패(컴파일·기능·인터페이스)거나 통과면 빈 문자열."""
    why = GRADER_DEFECT_STATUS.get(r.get("status") or "")
    return f'{r.get("status")}: {why}. {str(r.get("reason", ""))[:300]}' if why else ""


def evaluate(item: BenchItem, sv_files: list[Path], eval_dir: Path, timeout: int = 1800) -> dict[str, Any]:
    """후보 SV 파일들을 한 파일로 합쳐 공식 결과 트리(`<root>/rtlf/<모듈>/<모듈>_t1.v`)에 두고 공식 verify를 돌린다.
    같은 이름 module이 둘이면 처음 것(제공 셀)만 남긴다."""
    eval_dir = Path(eval_dir).resolve()
    des = Path(item.reference_rtl[0]).parent
    name = ("mips_" if des.parent.name == "mips_16" else "") + des.name
    # 방출 RTL의 SVA는 `ifndef SYNTHESIS 안에 있다. iverilog는 동시 assertion을 읽지 못하므로 RTLLM 채점(-DSYNTHESIS)처럼 끈다
    body, dropped = dedupe_modules("\n\n".join(Path(p).read_text(encoding="utf-8") for p in sv_files))
    text = "`define SYNTHESIS\n" + body
    r, adir, log = _verify(text, name, eval_dir, timeout)
    if not r:
        return {"ok": False, "kind": "chipverilog", "stage": "tool_error", "log_tail": log[-1000:],
                "grader_defect": "공식 도구가 결과를 남기지 않음 (채점 도구 오류)"}
    ok = r.get("status") == "pass"
    # 등가 실패면 증명되지 않은 출력 이름을 남긴다 (Repair가 볼 단서. 공식 반례 요약은 배너뿐인 때가 많다)
    unproven = []
    if (eq := adir / "equivalence.log").exists():
        for m in re.finditer(r"Unproven \$equiv \S+: \\(\S+?)_gold(?: \[(\d+)\])?", eq.read_text(errors="replace")):
            if m.group(1) not in unproven:
                unproven.append(m.group(1))
    stage = {"compile_fail": "compile"}.get(r.get("status"), "sim")
    try:   # 공식 도구가 정답 쪽에서 찾지 못해 스텁으로 바꾼 하위 모듈 (JSON 문자열). 있으면 채점 기준이 정답이 아니다
        stubs = json.loads(r.get("generated_stub_modules") or "[]")
    except (TypeError, ValueError):
        stubs = []
    # 자체 검사 TB 실패면 값 목록만으로는 Repair가 지연 문제를 보지 못한다. 출력 패턴을 읽어 한 줄 진단을 앞에 붙인다
    diag = ""
    if not ok and (sl := adir / "sim.log").exists():
        tbs = sorted(p for p in des.glob("*.v") if re.search(r"tb|test", p.name, re.I))
        diag = diagnose_sim(sl.read_text(errors="replace"), tbs[0].read_text(errors="replace") if tbs else "")
    # 등가로만 채점하는 문제는 TB가 없어 Repair·외부 수정이 볼 근거가 없다. 정답과 후보를 같은 무작위 입력으로 돌려 첫 불일치를 반례로 준다
    if not ok and r.get("flow") == "equivalence" and r.get("status") == "function_fail" and not stubs:
        try:
            cex = counterexample(item, text, eval_dir / "cex")
        except (OSError, subprocess.SubprocessError, ValueError):
            cex = ""
        if cex:
            (eval_dir / "counterexample.log").write_text(cex + "\n", encoding="utf-8")
            diag = (diag + "\n" if diag else "") + cex
    return {"ok": ok, "kind": "chipverilog", "status": r.get("status"), "flow": r.get("flow"), "proof_type": r.get("proof_type"),
            "stage": stage, "unproven_outputs": unproven, "deduped_modules": dropped, "diagnosis": diag, "stub_modules": stubs,
            "grader_defect": grader_defect(r),
            "log_tail": "" if ok else (f'{r.get("status")}: ' + (diag + " " if diag else "")
                                       + (f"정답의 하위 모듈 {stubs}이(가) 데이터셋에 없어 공식 도구의 스텁으로 채점됨 (채점 결함). " if stubs else "")
                                       + (f"정답과 등가가 증명되지 않은 출력 {unproven[:20]} (사이클 단위 비교). " if unproven else "")
                                       + f'{r.get("reason", "")} {r.get("counterexample_summary", "")}')[:1500 + len(diag)]}


CEX_CYCLES = 300
CEX_SEEDS = ((1, 0.0), (2, 0.04), (3, 0.04))   # (난수 씨앗, 리셋을 다시 거는 확률). 처음 2사이클은 늘 리셋


def _width(w: str) -> int:
    m = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", w.strip()) if w else None
    return abs(int(m.group(1)) - int(m.group(2))) + 1 if m else (1 if not w.strip() else 64)


def counterexample(item: BenchItem, cand_text: str, work: Path) -> str:
    """정답 RTL과 후보를 같은 무작위 입력(처음 리셋, 이후 가끔 리셋)으로 따로 시뮬레이션해 사이클마다 출력을 비교한다.
    첫 불일치와 그 전후 입력·기대·실제 값을 요약한다 (정답 코드는 주지 않는다). 공식 판정(유한 깊이 등가)과 같은 사이클 단위 비교다."""
    import random
    import shutil
    from experiments.bench.common import mismatch_trace
    from experiments.bench.common.to_spec import port_reset_traits
    if not shutil.which("iverilog"):
        return ""
    ports = _decl_ports(item.header_sv, item.top) or []
    ins = [(n, _width(w)) for n, d, w in ports if d == "input"]
    outs = [(n, _width(w)) for n, d, w in ports if d == "output"]
    if not outs:
        return ""
    clocks = [n for n, w in ins if w == 1 and re.search(r"(?:^|_)(?:clk|clock)(?:_|$)|^clk|clk$", n, re.I)]
    resets = {}
    for n, w in ins:
        if w == 1 and n not in clocks and re.search(r"rst|reset", n, re.I):
            active, _ = port_reset_traits(item.prompt_text, n)
            resets[n] = active or ("low" if re.search(r"(?:_n|n)$|^n[A-Z_]", n) else "high")
    data = [(n, w) for n, w in ins if n not in clocks and n not in resets]
    des = Path(item.reference_rtl[0]).parent
    incs = [f"-I{d}" for d in [*item.incdirs, str(des), str(des.parent)]]
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "cand.v").write_text(cand_text, encoding="utf-8")
    best: tuple[int, str] | None = None
    for seed, p_rst in CEX_SEEDS:
        rnd = random.Random(seed)
        stim = []
        for c in range(CEX_CYCLES):
            v = {n: format(rnd.getrandbits(w), f"0{w}b") for n, w in data}
            for n, a in resets.items():
                on = c < 2 or rnd.random() < p_rst
                v[n] = "1" if on == (a == "high") else "0"
            stim.append(v)
        decl = "".join(f"  reg {f'[{w - 1}:0] ' if w > 1 else ''}{n};\n" for n, w in ins) \
            + "".join(f"  wire {f'[{w - 1}:0] ' if w > 1 else ''}{n};\n" for n, w in outs)
        conn = ", ".join(f".{n}({n})" for n, _, _ in ports if any(n == x for x, _ in ins + outs))
        body = "".join("    " + " ".join(f"{n} = {len(b)}'b{b};" for n, b in v.items())
                       + f" #3 $fdisplay(fd, \"{' '.join(['%b'] * len(outs))}\", {', '.join(n for n, _ in outs)}); #7;\n" for v in stim)
        tb = ("`timescale 1ns/1ps\nmodule rtlf_cex_tb;\n" + decl + f"  {item.top} dut({conn});\n  integer fd;\n"
              + "".join(f"  initial {c} = 1'b0;\n  always #5 {c} = ~{c};\n" for c in clocks)
              + "  initial begin\n    fd = $fopen(\"out.txt\", \"w\");\n    #1;\n" + body + "    $fclose(fd);\n    $finish;\n  end\nendmodule\n")
        (work / "tb.v").write_text(tb, encoding="utf-8")
        res = {}
        for side, src in (("ref", item.reference_rtl[0]), ("cand", str(work / "cand.v"))):
            d = work / f"s{seed}_{side}"
            d.mkdir(exist_ok=True)
            comp = subprocess.run(["iverilog", "-g2012", *incs, "-o", str(d / "sim"), "-s", "rtlf_cex_tb", str(work / "tb.v"), src],
                                  capture_output=True, text=True, timeout=300)
            if comp.returncode != 0:
                return ""
            subprocess.run(["vvp", "-n", str(d / "sim")], cwd=d, capture_output=True, text=True, timeout=300)
            res[side] = (d / "out.txt").read_text(encoding="utf-8", errors="replace").split("\n") if (d / "out.txt").exists() else []
        rows = []
        for c, (le, la) in enumerate(zip(res["ref"], res["cand"])):
            e, a = le.split(), la.split()
            if len(e) != len(outs) or len(a) != len(outs):
                break
            rows.append({"cyc": c, "time": None, "ins": {n: stim[c][n] for n in [*resets, *(x for x, _ in data)]},
                         "exp": {n: e[k] for k, (n, _) in enumerate(outs)}, "act": {n: a[k] for k, (n, _) in enumerate(outs)}})
        first = next((i for i, r in enumerate(rows) if any(mismatch_trace.differs(r["exp"][o], r["act"][o]) for o in r["exp"])), None)
        if first is not None and (best is None or first < best[0]):
            src = f"정답과 후보에 같은 무작위 입력, 씨앗 {seed}, 처음 2사이클 리셋" + (f", 이후 사이클마다 {p_rst:.0%} 확률로 리셋" if p_rst else "")
            best = (first, mismatch_trace.summarize(rows, clocks[0] if clocks else "clk", src))
    return best[1] if best else ""


def official_verdict(rec: dict, rtl_code: str, work_dir: Path) -> dict[str, Any]:
    """CVDP 레코드 하나와 아무 엔진의 생성 RTL 문자열을 공식 도구로 채점한다 (엔진 간 비교에서 모든 엔진에 같은 판정).
    CVDP 변환본이 덧붙인 `include 파일(예: defines.vh)은 공식 도구에 없으므로 레코드의 같은 이름 파일 내용으로 펼친다."""
    work_dir = Path(work_dir).resolve()
    files = {**rec.get("context", {}), **rec["harness"]["files"]}   # include 대상은 harness, 없으면 context에서 찾는다

    def inline(m: re.Match) -> str:
        name = m.group(1)
        return next((v for k, v in files.items() if k == name or k.endswith("/" + name)), m.group(0))

    if not rtl_code.strip():
        return {"ok": False, "kind": "chipverilog", "status": "no_rtl", "log_tail": "생성 RTL 없음"}
    work_dir.mkdir(parents=True, exist_ok=True)
    cand = work_dir / "cand.v"
    cand.write_text(re.sub(r'`include\s+"([^"]+)"', inline, rtl_code), encoding="utf-8")
    return evaluate(to_item(rec, work_dir / "src"), [cand], work_dir / "eval")
