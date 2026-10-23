"""PDF SPEC → Markdown 변환. 표와 제목 구조는 배치(글자 크기, 굵기, 절 번호, 선)에서 추정한다.

PDF에는 구조 정보가 없으므로 결과 전체를 사람이 검토한다. 이 툴은 검토를 돕는 리포트를 함께 낸다.
- 제목: 본문보다 큰 글자이거나 굵은 글자이면서 절 번호(`4.3.2`)로 시작하는 줄. PDF에 목차(outline)가 있으면
  추정한 제목과 대조해 일치율을 낸다 (목차는 검증에만 쓰고 변환에는 쓰지 않는다)
- 표: 선으로 그린 표를 찾는다. 행마다 칸 수가 다르거나 빈 칸이 많은 표는 검토 대상으로 표시한다
- 그림: `Figure N` 캡션과 쪽 번호. 타이밍 다이어그램 변환(waveform.py)의 후보 목록이 된다
- 텍스트가 거의 없는 쪽: 스캔본으로 보고 OCR이 필요하다고 표시한다 (OCR은 하지 않음)
- 10개 섹션 템플릿: 제목에서 템플릿 섹션 이름을 찾아 대응표를 만든다. 템플릿을 따르지 않는 문서는 대응이 비고,
  사람이 대응표를 써야 한다

    python3 -m pipeline.spec.pdf <spec.pdf> -o <dir> [--pages 37-80]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import pymupdf
except ImportError:  # 예전 이름
    import fitz as pymupdf

from pipeline.spec.parse import TEMPLATE

_NUM = re.compile(r"^(\d+(?:\.\d+){0,5})\.?\s+(\S.*)$")
_FIG = re.compile(r"^(Figure|그림)\s+(\d+)[.:]?\s+(.*)$")
_TAB = re.compile(r"^(Table|표)\s+(\d+)[.:]?\s+(.*)$")
SCANNED_CHARS = 50


def _raw_lines(page) -> list[dict]:
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            out.append({"text": "".join(s["text"] for s in spans).strip(), "size": max(round(s["size"], 1) for s in spans),
                        "bold": bool(all(("Bold" in s["font"]) or (s["flags"] & 16) for s in spans)),
                        "y": ln["bbox"][1], "bbox": list(ln["bbox"])})
    return out


def _lines(page, drop: set | None = None) -> list[dict]:
    """화면의 한 줄 단위로 합친다. PDF는 절 번호와 제목을 같은 높이의 다른 줄로 두는 경우가 많다.
    drop은 쪽마다 반복되는 머리말·꼬리말 (y, 텍스트)."""
    raw = [ln for ln in _raw_lines(page) if not drop or (round(ln["y"]), ln["text"]) not in drop]
    raw.sort(key=lambda ln: (round(ln["y"]), ln["bbox"][0]))
    out: list[dict] = []
    for ln in raw:
        prev = out[-1] if out else None
        if prev and abs(prev["y"] - ln["y"]) < 2 and prev["size"] == ln["size"] and prev["bold"] == ln["bold"]:
            prev["text"] += " " + ln["text"]
            prev["bbox"] = [min(prev["bbox"][0], ln["bbox"][0]), min(prev["bbox"][1], ln["bbox"][1]),
                            max(prev["bbox"][2], ln["bbox"][2]), max(prev["bbox"][3], ln["bbox"][3])]
        else:
            out.append(dict(ln))
    return out


def _running(doc, pages) -> set:
    """쪽의 30% 이상에서 같은 높이에 같은 글로 나오는 줄 (머리말, 꼬리말)."""
    c = Counter()
    for i in pages:
        for k in {(round(ln["y"]), ln["text"]) for ln in _raw_lines(doc[i])}:
            c[k] += 1
    return {k for k, n in c.items() if n >= max(3, 0.3 * len(pages))}


def _body_size(doc, pages, drop) -> float:
    c = Counter()
    for i in pages:
        for ln in _lines(doc[i], drop):
            c[ln["size"]] += len(ln["text"])
    return c.most_common(1)[0][0] if c else 10.0


def _md_table(rows: list[list]) -> str:
    rows = [[("" if c is None else str(c)).replace("\n", " ").replace("|", "\\|").strip() for c in r] for r in rows]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def convert(path: str | Path, pages: range | None = None) -> dict:
    doc = pymupdf.open(path)
    pages = pages or range(doc.page_count)
    drop = _running(doc, pages)
    body = _body_size(doc, pages, drop)
    md: list[str] = []
    headings, tables, figures, scanned, ragged = [], [], [], [], []
    for i in pages:
        page = doc[i]
        # 여백의 줄 번호(본문보다 작은 숫자만 있는 줄)도 뺀다
        lines = [ln for ln in _lines(page, drop) if not (ln["text"].isdigit() and ln["size"] < body)]
        if sum(len(ln["text"]) for ln in lines) < SCANNED_CHARS:
            scanned.append(i + 1)
        try:
            found = page.find_tables().tables
        except Exception:
            found = []
        tboxes = [t.bbox for t in found]

        def in_table(bb):
            return any(b[0] - 1 <= bb[0] and bb[2] <= b[2] + 1 and b[1] - 1 <= bb[1] and bb[3] <= b[3] + 1 for b in tboxes)

        emitted = set()
        para: list[str] = []
        for ln in lines:
            if in_table(ln["bbox"]):
                idx = next(k for k, b in enumerate(tboxes) if b[1] - 1 <= ln["bbox"][1] <= b[3] + 1)
                if idx not in emitted:
                    if para:
                        md.append(" ".join(para))
                        para = []
                    rows = found[idx].extract()
                    widths = {sum(1 for c in r if c not in (None, "")) for r in rows}
                    tables.append({"page": i + 1, "rows": len(rows), "cols": max(len(r) for r in rows) if rows else 0})
                    if len(widths) > 2 or any(all(c in (None, "") for c in r) for r in rows):
                        ragged.append({"page": i + 1, "table": len(tables), "reason": "행마다 채워진 칸 수가 다르거나 빈 행이 있음"})
                    md.append(_md_table(rows) if rows else "")
                    emitted.add(idx)
                continue
            m = _NUM.match(ln["text"])
            if m and (ln["size"] > body + 0.5 or ln["bold"]) and len(ln["text"]) < 120:
                if para:
                    md.append(" ".join(para))
                    para = []
                level = min(m.group(1).count(".") + 2, 6)
                headings.append({"page": i + 1, "number": m.group(1), "title": m.group(2), "level": level})
                md.append("#" * level + " " + ln["text"])
                continue
            f = _FIG.match(ln["text"])
            if f and ln["bold"]:
                figures.append({"page": i + 1, "number": int(f.group(2)), "caption": f.group(3), "bbox": ln["bbox"]})
            para.append(ln["text"])
        if para:
            md.append(" ".join(para))
    report = {"pdf": str(path), "pages": [pages[0] + 1, pages[-1] + 1], "body_font": body,
              "headings": len(headings), "tables": len(tables), "figures": len(figures),
              "scanned_pages": scanned, "ragged_tables": ragged}
    toc = [t for t in doc.get_toc() if pages[0] + 1 <= t[2] <= pages[-1] + 1]
    if toc:
        def norm(t):  # "1. 제목"과 "1 제목"을 같게 본다
            return re.sub(r"^(\d+(?:\.\d+)*)\.?\s+", r"\1 ", re.sub(r"\s+", " ", t).strip())

        found_titles = {norm(h["number"] + " " + h["title"]) for h in headings}
        toc_titles = [norm(t[1]) for t in toc if _NUM.match(t[1])]
        missed = [t for t in toc_titles if t not in found_titles]
        report["toc_check"] = {"toc_numbered": len(toc_titles), "matched": len(toc_titles) - len(missed),
                               "ratio": round(1 - len(missed) / len(toc_titles), 3) if toc_titles else None,
                               "missed": missed[:30]}
    sec_map = {}
    for n, name in TEMPLATE.items():
        h = next((h for h in headings if name in h["title"]), None)
        sec_map[n] = f'{h["number"]} {h["title"]} (p.{h["page"]})' if h else None
    report["template_map"] = sec_map
    report["template_matched"] = sum(v is not None for v in sec_map.values())
    return {"markdown": "\n\n".join(md) + "\n", "headings": headings, "tables": tables, "figures": figures, "report": report}


def review_markdown(r: dict) -> str:
    rep = r["report"]
    out = [f"# PDF 변환 검토: {Path(rep['pdf']).name}", "", "변환 결과 전체를 사람이 검토합니다. 아래는 먼저 볼 곳입니다.", "",
           f"- 쪽: {rep['pages'][0]}\\~{rep['pages'][1]}, 본문 글자 크기 {rep['body_font']}",
           f"- 추정한 제목 {rep['headings']}개, 표 {rep['tables']}개, 그림 캡션 {rep['figures']}개"]
    if "toc_check" in rep:
        t = rep["toc_check"]
        out.append(f"- PDF 목차와 대조: 번호 붙은 목차 항목 {t['toc_numbered']}개 중 {t['matched']}개를 제목으로 찾음 ({t['ratio']})")
    out.append(f"- 10개 섹션 템플릿 대응: {rep['template_matched']}/10" + (" (템플릿을 따르지 않는 문서. 대응표를 사람이 작성)" if rep["template_matched"] < 10 else ""))
    out.append(f"- 텍스트가 거의 없는 쪽 (빈 쪽이거나 스캔본. 스캔본이면 OCR 필요): {rep['scanned_pages'] or '없음'}")
    if rep["ragged_tables"]:
        out += ["", "## 모양이 고르지 않은 표", "", "| 쪽 | 표 번호 | 이유 |", "| --- | --- | --- |"]
        out += [f"| {x['page']} | {x['table']} | {x['reason']} |" for x in rep["ragged_tables"]]
    if rep.get("toc_check", {}).get("missed"):
        out += ["", "## 목차에는 있는데 제목으로 찾지 못한 항목", ""] + [f"- {m}" for m in rep["toc_check"]["missed"]]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PDF SPEC → Markdown")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--pages", help="예: 37-80 (1부터)")
    args = ap.parse_args(argv)
    pages = None
    if args.pages:
        a, b = (int(x) for x in args.pages.split("-"))
        pages = range(a - 1, b)
    r = convert(args.pdf, pages)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(args.pdf).stem
    (out / f"{stem}.md").write_text(r["markdown"], encoding="utf-8")
    (out / f"{stem}.figures.json").write_text(json.dumps(r["figures"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{stem}.review.json").write_text(json.dumps(r["report"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{stem}.review.md").write_text(review_markdown(r), encoding="utf-8")
    print(review_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
