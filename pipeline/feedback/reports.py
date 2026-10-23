"""외부 검사 리포트 파서. 툴마다 양식 파일(`config/report_formats/<이름>.json`)로 정한다.

양식 파일만 더하거나 고치면 새 툴의 리포트를 받을 수 있다. 코드를 고칠 필요가 없다. 형식은 README.md를 본다.
결과는 Verify와 같은 `Finding`(툴, 규칙, 파일, 줄, 메시지, 심각도)이므로 Classifier가 소스맵으로 IR 노드에 대응시킨다.

    python3 -m pipeline.feedback.reports <양식 이름> <리포트 파일>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pipeline.verify.run import Finding

ROOT = Path(__file__).resolve().parents[2]
FORMATS = ROOT / "config" / "report_formats"
_FIELDS = ("severity", "rule", "file", "line", "message")


class FormatError(ValueError):
    pass


def formats(root: Path = FORMATS) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(root).glob("*.json"))}


def check_format(fmt: dict) -> list[str]:
    """양식 파일의 형식 오류."""
    errs = []
    if not isinstance(fmt.get("tool"), str) or not fmt["tool"]:
        errs.append("tool(툴 이름)이 필요함")
    pats = fmt.get("patterns")
    if not isinstance(pats, list) or not pats:
        errs.append("patterns(정규식 목록)가 하나 이상 필요함")
        return errs
    for i, p in enumerate(pats):
        try:
            rx = re.compile(p["regex"])
        except (KeyError, TypeError, re.error) as e:
            errs.append(f"patterns[{i}].regex: {e}")
            continue
        if "message" not in rx.groupindex and "message" not in p.get("defaults", {}):
            errs.append(f"patterns[{i}]: message 그룹이나 기본값이 필요함")
        extra = set(rx.groupindex) - set(_FIELDS)
        if extra:
            errs.append(f"patterns[{i}]: 모르는 그룹 {sorted(extra)} (쓸 수 있는 것: {list(_FIELDS)})")
    for i, rx in enumerate(fmt.get("ignore", [])):
        try:
            re.compile(rx)
        except re.error as e:
            errs.append(f"ignore[{i}]: {e}")
    return errs


def parse(fmt: dict, text: str) -> list[Finding]:
    """리포트 텍스트 → Finding 목록. 줄마다 patterns를 순서대로 맞춰 보고, 처음 맞은 것을 쓴다.
    continuation이 있으면 그 정규식에 맞는 줄을 직전 결과의 메시지에 이어 붙인다."""
    errs = check_format(fmt)
    if errs:
        raise FormatError("; ".join(errs))
    pats = [(re.compile(p["regex"]), p.get("defaults", {})) for p in fmt["patterns"]]
    ignore = [re.compile(x) for x in fmt.get("ignore", [])]
    cont = re.compile(fmt["continuation"]) if fmt.get("continuation") else None
    sev_map = {str(k).lower(): v for k, v in fmt.get("severity_map", {}).items()}
    out: list[Finding] = []
    for ln in text.splitlines():
        if any(r.search(ln) for r in ignore):
            continue
        for rx, defaults in pats:
            m = rx.search(ln)
            if not m:
                continue
            g = {**defaults, **{k: v for k, v in m.groupdict().items() if v is not None}}
            sev = sev_map.get(str(g.get("severity", "error")).lower(), str(g.get("severity", "error")).lower())
            line = g.get("line")
            out.append(Finding(fmt["tool"], str(g.get("rule") or fmt.get("default_rule", "REPORT")), g.get("file"),
                               int(line) if line not in (None, "") else None, str(g.get("message", "")).strip(),
                               "error" if sev not in ("error", "warning", "info") else sev))
            break
        else:
            if cont and out and cont.match(ln):
                f = out[-1]
                out[-1] = Finding(f.tool, f.rule, f.file, f.line, f"{f.message} {ln.strip()}", f.severity, f.test)
    return out


def parse_named(name: str, text: str, root: Path = FORMATS) -> list[Finding]:
    fmts = formats(root)
    if name not in fmts:
        raise FormatError(f"양식 파일이 없음: {name} (있는 것: {sorted(fmts)})")
    return parse(fmts[name], text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="외부 검사 리포트 파서")
    ap.add_argument("format")
    ap.add_argument("report", type=Path)
    a = ap.parse_args(argv)
    try:
        fs = parse_named(a.format, a.report.read_text(encoding="utf-8", errors="replace"))
    except FormatError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps([f.__dict__ for f in fs], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
