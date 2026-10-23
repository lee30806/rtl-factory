"""P11 R1: 원본 RTL → 모듈별 L1·L0 초안, 원문, 거부 목록, 검토 항목.

    python3 -m pipeline.importer.r1 <파일...> --top <모듈> [-f filelist.f] [-I dir] [-D NAME=V] [-G NAME=V] [--lang verilog|sv] -o <폴더>

결과 (`<폴더>/r1/`):
- `ir/<모듈>.ir.json`: L1(params, domains, ports)과 L0(instances, connections, 연결에 쓰는 wire 신호) 초안
- `source/<모듈>.sv`: 원본 모듈 글자 (Porter 입력). 헤더는 `source/headers/`
- `sim_src/`: 원본 시뮬레이션용 사본 (generate 배열 안 인스턴스의 defparam을 인스턴스 파라미터로 옮김. 필요할 때만)
- `import.json`: 입력, 모듈 순서(하위부터), 모듈별 원본 위치·이름 바꿈·마무리할 연결·매크로에서 온 localparam·포트 도메인,
  거부 목록(`rejects`), 검토 항목(`reviews`), V1~V3 결과
거부 항목이 있으면 종료 코드 1이다.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from pipeline.importer.extract import Extractor
from pipeline.importer.sources import Design, Inputs, SourceError, read_filelist
from pipeline.ir.model import Module
from pipeline.validator.checks import errors, validate


def run(inp: Inputs, out: Path) -> dict:
    out = Path(out)
    r1 = out / "r1"
    if r1.exists():
        shutil.rmtree(r1)
    (r1 / "ir").mkdir(parents=True)
    (r1 / "source").mkdir()
    try:
        design = Design(inp)
    except SourceError as e:
        res = {"inputs": inp.to_dict(), "ok": False, "rejects": e.items, "reviews": [], "modules": {}, "order": []}
        (r1 / "import.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        return res
    ext = Extractor(design).run()
    from pipeline.importer import simcopy
    sim_units = simcopy.make(design, r1 / "sim_src")   # Verilator가 받지 못하는 구문을 바꾼 원본 사본 (R4/R6)
    mods = ext["modules"]
    meta = {}
    for name in ext["order"]:
        m = mods[name]
        (r1 / "ir" / f"{name}.ir.json").write_text(json.dumps(m.ir, ensure_ascii=False, indent=2), encoding="utf-8")
        (r1 / "source" / f"{name}.sv").write_text(m.source["text"], encoding="utf-8")
        meta[name] = {"file": m.source["file"], "line": m.source["line"], "end_line": m.source["end_line"],
                      "renames": m.renames, "pending": m.pending, "macro_params": m.macro_params,
                      "port_domains": m.port_domains, "tri_split": m.tri_split, "removed_params": m.removed_params}
    if design.headers:
        (r1 / "source" / "headers").mkdir()
        for h in design.headers:
            shutil.copy(h, r1 / "source" / "headers" / h.name)
    lib = {n: Module(mods[n].ir) for n in ext["order"]}
    issues = {n: [str(i) for i in errors(validate(lib[n], lib, upto=3))] for n in ext["order"]}
    res = {"inputs": inp.to_dict(), "top": ext["top"], "order": ext["order"], "modules": meta,
           "headers": [str(h) for h in design.headers], "units": [str(u) for u in design.units],
           "wrapped": sorted(design.wrapped), "sim_units": sim_units,
           "macros": {k: {"value": v.value, "file": v.file, "line": v.line, "numeric": v.numeric} for k, v in design.macros.items()},
           "rejects": ext["rejects"], "reviews": ext["reviews"], "validate": issues,
           "ok": not ext["rejects"] and not any(issues.values())}
    (r1 / "import.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def parse_args(argv=None) -> tuple[Inputs, Path]:
    ap = argparse.ArgumentParser(description="P11 R1: 원본 RTL에서 L1·L0 초안 추출")
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--top", required=True)
    ap.add_argument("-f", dest="filelists", action="append", type=Path, default=[])
    ap.add_argument("-I", dest="incdirs", action="append", type=Path, default=[])
    ap.add_argument("-D", dest="defines", action="append", default=[])
    ap.add_argument("-G", dest="params", action="append", default=[])
    ap.add_argument("--lang", choices=["verilog", "sv"])
    ap.add_argument("-o", "--out", required=True, type=Path)
    a = ap.parse_args(argv)
    files, incs, defs = list(a.files), list(a.incdirs), {}
    for fl in a.filelists:
        f2, i2, d2 = read_filelist(fl)
        files += f2
        incs += i2
        defs.update(d2)
    for d in a.defines:
        k, _, v = d.partition("=")
        defs[k] = v or "1"
    params = dict(p.split("=", 1) for p in a.params)
    return Inputs(files, a.top, params, incs, defs, a.lang), a.out


def main(argv=None) -> int:
    inp, out = parse_args(argv)
    res = run(inp, out)
    print(json.dumps({"ok": res["ok"], "order": res.get("order"), "rejects": res["rejects"],
                      "reviews": len(res.get("reviews", [])), "validate": res.get("validate", {})}, ensure_ascii=False, indent=2))
    return 0 if not res["rejects"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
