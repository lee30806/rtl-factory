"""타이밍 다이어그램 → WaveJSON. 비전 모델을 쓰는 coding agent가 변환하고 사람이 대조한다.

- 그림: PDF 쪽에서 `Figure N` 캡션 위의 그림 영역을 잘라 PNG로 만든다 (Word는 원본 그림 파일을 씀)
- 변환: 그림과 TASK.md를 작업 디렉터리에 두고 coding agent를 headless로 실행해 `out/wave.json`을 받는다
- 캐시: 키는 그림 바이트의 해시와 모델. 그림이 바뀌지 않으면 다시 변환하지 않는다
- 대조: 변환 결과를 다시 그려(`wavejson.render`) 원본과 나란히 둔 검토 페이지를 만든다 (사람이 전부 확인)
- 태그: 그림마다 작성자가 규범/예시, 설명, 요구사항 ID(규범) 또는 시나리오 ID(예시)를 붙인다.
  수정할 수 없는 PDF 원본은 그림별 주석 파일(JSON)에 쓴다. 규범인데 6번 섹션의 요구사항이 없으면 오류

    python3 -m pipeline.spec.waveform <spec.pdf> --figures 14,16 --agents <agents.json> --agent <이름> -o <dir>
        [--spec spec.md --annotations figures.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from pipeline.agents.runner import AgentConfig, load_agents, run_agent
from pipeline.spec.issues import SpecIssue
from pipeline.spec.wavejson import expand, flatten, parse_wavejson, render

TASK_VERSION = "1"
TASK = """\
# 과제: 타이밍 다이어그램을 WaveJSON으로 변환

`{image}`은 디지털 회로 규격서의 타이밍 다이어그램이다. 그림을 보고 WaveDrom의 WaveJSON으로 변환해 `out/wave.json`에 쓴다.

## 규칙
- 파일 내용은 {{"signal": [...]}} 형태의 JSON 객체 하나다.
- 신호 이름은 그림에 적힌 그대로 쓴다.
- wave 문자열의 한 글자가 한 클럭 사이클(클럭이 없으면 그림의 한 구간)이다. 모든 신호의 wave 길이를 같게 맞춘다.
- 클럭은 p(상승 에지 시작)와 '.', 0/1 레벨은 0과 1, 값이 바뀌는 버스는 '='와 data 라벨, 알 수 없는 값은 x, 고임피던스나 놓인 선은 z를 쓴다.
- 앞과 같은 값이 이어지면 '.'을 쓴다.
- 그림에서 읽을 수 없는 부분은 지어내지 말고 x로 둔다.
- `out/wave.json` 외의 파일은 만들거나 고치지 않는다.
"""


def image_key(image: Path, cfg: AgentConfig) -> str:
    h = hashlib.sha256(image.read_bytes())
    h.update(f"|{cfg.runtime}|{cfg.model}|{TASK_VERSION}".encode())
    return h.hexdigest()[:20]


def lint_wavejson(wj: dict) -> list[str]:
    """형식 확인: 신호 목록, 이름, 같은 길이."""
    sigs = list(flatten(wj).values())
    if not sigs:
        return ["신호가 없음"]
    out = []
    lens = {s.get("name", "?"): len(expand(s)) for s in sigs}
    if len(set(lens.values())) > 1:
        out.append(f"신호마다 사이클 수가 다름: {lens}")
    if any(not s.get("name") for s in sigs):
        out.append("이름 없는 신호가 있음")
    return out


def convert(image: Path, cfg: AgentConfig, cache: Path) -> dict:
    """그림 하나를 변환한다. 캐시에 있으면 에이전트를 부르지 않는다."""
    cache.mkdir(parents=True, exist_ok=True)
    key = image_key(image, cfg)
    hit = cache / f"{key}.json"
    if hit.exists():
        return {**json.loads(hit.read_text(encoding="utf-8")), "cached": True}
    work = cache / "work" / key
    if work.exists():
        shutil.rmtree(work)
    (work / "out").mkdir(parents=True)
    shutil.copy(image, work / image.name)
    run = run_agent(cfg, work, TASK.format(image=image.name), extra={"image": image.name})
    res = {"key": key, "image": str(image), "agent": cfg.name, "model": cfg.model, "duration_s": round(run.duration_s, 1),
           "exit_ok": run.exit_ok, "wavejson": None, "issues": []}
    out = work / "out" / "wave.json"
    if not out.exists():
        res["issues"].append("결과 파일 out/wave.json이 없음")
    else:
        try:
            res["wavejson"] = parse_wavejson(out.read_text(encoding="utf-8"))
            res["issues"] += lint_wavejson(res["wavejson"])
        except Exception as e:
            res["issues"].append(f"WaveJSON을 읽을 수 없음: {e}")
    if res["wavejson"] and not res["issues"]:
        render(res["wavejson"], cache / f"{key}.png")
        res["render"] = str(cache / f"{key}.png")
        hit.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")  # 성공한 것만 캐시
    return {**res, "cached": False}


def extract_figure(pdf: Path, page_no: int, caption_y: float, out_png: Path, zoom: float = 2.0) -> Path:
    """캡션 바로 위의 그림 영역을 잘라 PNG로 만든다. 캡션에서 위로 붙어 있는 도형과 그 안의 글자를 모은다."""
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf

    page = pymupdf.open(pdf)[page_no - 1]
    rects = [d["rect"] for d in page.get_drawings() if d["rect"].y1 <= caption_y + 2 and d["rect"].height < 500]
    rects += [pymupdf.Rect(r) for img in page.get_images() for r in page.get_image_rects(img[0]) if r.y1 <= caption_y + 2]
    if not rects:
        raise ValueError(f"{page_no}쪽 캡션 위에 그림이 없음")
    # 그림은 대부분 폭이나 높이가 0인 선이다. Rect의 합집합(|)은 넓이가 0인 사각형을 무시하므로 좌표로 직접 합친다.
    rects.sort(key=lambda r: -r.y1)
    x0, y0, x1, y1 = rects[0].x0, rects[0].y0, rects[0].x1, rects[0].y1
    for r in rects[1:]:  # 위로 30pt 이내로 이어지는 것만
        if r.y1 >= y0 - 30:
            x0, y0, x1, y1 = min(x0, r.x0), min(y0, r.y0), max(x1, r.x1), max(y1, r.y1)
    for b in page.get_text("dict")["blocks"]:  # 그림 안의 신호 이름 등
        bx0, by0, bx1, by1 = b["bbox"]
        if by0 >= y0 - 5 and by1 <= caption_y and bx1 >= x0 - 80 and bx0 <= x1:
            x0, y0, x1, y1 = min(x0, bx0), min(y0, by0), max(x1, bx1), max(y1, by1)
    box = pymupdf.Rect(x0, y0, x1, y1)
    box = (box + (-6, -6, 6, 6)) & page.rect
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=box).save(out_png)
    return out_png


def check_annotations(spec, annotations: list[dict], fname: str = "figures.json") -> list[SpecIssue]:
    """규범 그림은 6번 섹션 요구사항과, 예시 그림은 8번 섹션 시나리오와 연결되어야 한다."""
    reqs = {r.id: r for r in spec.requirements}
    scs = {s["id"] for s in spec.scenarios}
    out = []
    for a in annotations:
        w = f"{fname}:Figure {a.get('figure')}"
        tag = a.get("tag")
        if tag not in ("규범", "예시"):
            out.append(SpecIssue("WAVE_TAG", "error", w, f"태그는 규범 또는 예시여야 함: {tag!r}"))
        if not a.get("description"):
            out.append(SpecIssue("WAVE_DESC", "error", w, "설명이 없음 (어느 부분이 요구이고 어느 부분이 한 가지 예인지)"))
        if tag == "규범":
            ids = a.get("reqs", [])
            if not ids:
                out.append(SpecIssue("WAVE_REQ", "error", w, "규범 그림인데 연결한 요구사항이 없음"))
            for rid in ids:
                r = reqs.get(rid)
                if r is None:
                    out.append(SpecIssue("WAVE_REQ", "error", w, f"없는 요구사항 {rid}"))
                elif r.section != 6:
                    out.append(SpecIssue("WAVE_REQ", "warning", w, f"{rid}는 6번 섹션(타이밍과 성능)의 요구사항이 아님"))
        if tag == "예시" and a.get("scenario") not in scs:
            out.append(SpecIssue("WAVE_SCENARIO", "warning", w, f"예시 그림이 8번 섹션 시나리오에 편입되지 않음: {a.get('scenario')!r}"))
    return out


def review_page(items: list[dict], out: Path) -> Path:
    """원본 그림과 다시 그린 그림을 나란히 둔 검토 페이지 (Markdown)."""
    lines = ["# 타이밍 다이어그램 변환 검토", "", "원본(왼쪽)과 변환 결과를 다시 그린 그림(오른쪽)을 비교합니다. 모든 그림을 사람이 확인합니다.", ""]
    for it in items:
        lines += [f"## Figure {it['figure']}: {it.get('caption', '')}", "",
                  f"- 쪽 {it.get('page')}, 에이전트 {it['agent']} ({it['model']}), {it['duration_s']}초, 캐시 {'사용' if it.get('cached') else '안 씀'}"]
        if it.get("annotation"):
            a = it["annotation"]
            lines.append(f"- 태그 {a.get('tag')}, 요구사항 {a.get('reqs', [])}, 시나리오 {a.get('scenario')}: {a.get('description')}")
        for i in it.get("issues", []):
            lines.append(f"- **문제:** {i}")
        lines += ["", "| 원본 | 변환 결과 |", "| --- | --- |",
                  f"| ![]({Path(it['image']).name}) | " + (f"![]({Path(it['render']).name}) |" if it.get("render") else "(없음) |"), ""]
        if it.get("wavejson"):
            lines += ["```json", json.dumps(it["wavejson"], ensure_ascii=False), "```", ""]
    p = out / "waveform_review.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def main(argv=None) -> int:
    from pipeline.spec.pdf import convert as pdf_convert

    ap = argparse.ArgumentParser(description="타이밍 다이어그램 → WaveJSON (coding agent)")
    ap.add_argument("pdf")
    ap.add_argument("--figures", required=True, help="그림 번호 목록. 예: 14,16")
    ap.add_argument("--pages", help="그림을 찾을 쪽 범위 (예: 80-100)")
    ap.add_argument("--agents", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--spec")
    ap.add_argument("--annotations")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    cfg = next(a for a in load_agents(args.agents) if a.name == args.agent)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pages = None
    if args.pages:
        a, b = (int(x) for x in args.pages.split("-"))
        pages = range(a - 1, b)
    figs = {f["number"]: f for f in pdf_convert(args.pdf, pages)["figures"]}
    anns = {a["figure"]: a for a in json.loads(Path(args.annotations).read_text())} if args.annotations else {}
    items = []
    for n in (int(x) for x in args.figures.split(",")):
        f = figs[n]
        png = extract_figure(Path(args.pdf), f["page"], f["bbox"][1], out / f"figure_{n}.png")
        r = convert(png, cfg, out / "cache")
        if r.get("render"):
            shutil.copy(r["render"], out / f"figure_{n}.wave.png")
            r["render"] = str(out / f"figure_{n}.wave.png")
        items.append({**r, "figure": n, "caption": f["caption"], "page": f["page"], "image": str(png), "annotation": anns.get(n)})
        took = "캐시 사용" if r["cached"] else f'{r["duration_s"]}초'
        print(f"Figure {n}: {took}, 문제 {r['issues'] or '없음'}")
    issues = []
    if args.spec and anns:
        from pipeline.spec.parse import load

        issues = check_annotations(load(args.spec), list(anns.values()), Path(args.annotations).name)
    (out / "waveform.json").write_text(json.dumps({"items": items, "issues": [i.to_dict() for i in issues]},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    print(review_page(items, out))
    return 0 if all(not it["issues"] for it in items) and not any(i.severity == "error" for i in issues) else 1


if __name__ == "__main__":
    sys.exit(main())
