#!/usr/bin/env python3
"""bookforge QC gate — the ONLY path from draft/ to final/.

Usage: python3 qc_gate.py <book_dir> [--strict-pages]
Gates (pagination.md §7):
  G10 quote   : (렌더 전) ::: pull 인용·콜아웃 수치가 챕터 본문에 실재 — 날조 차단
  G0  svg     : (렌더 전) 도해 SVG 소스 — foreignObject 잔존/텍스트 부재/외부참조/
                단독문단 위반/사이드카 쌍 무결성/아이콘 탈락 차단
  G13 figtext : (렌더 후) 도해 라벨(fig-*.labels.json)이 PDF 실텍스트에 존재 —
                usvg의 조용한 텍스트 드롭 최종 포착
  G1  render  : draft/book.pdf exists; trim=tokens.trim_mm; page count WARN
                (INV-1 — hard only with --strict-pages)
  G2  fonts   : every font fully embedded
  G3  overflow: no bbox escapes the page rect (tol 1.5pt)
  G4  toc     : bookmarks match chapter start pages
  G7  density : FRAME(판면 드리프트)·BLANK(구 G5 흡수)·TAIL·MID·DOC — reach/ink/gap
  G8  stretch : 공기 채움(gap) + 행송 편차 탐지
  G9  keep    : 면 끝 제목 고립 / widow (단일단 스타일)
  G11 roles   : pageroles.json 무결성(코드·선행조건·anchor·예산)
  G12 parity  : 장 시작 직전 필러 백면 (단면 전자책에 인쇄 관습 금지)
  G14 toc     : (tocgate.py) A 인쇄 목차 쪽번호↔폴리오 자기일관 / B 목차↔도비라
                색상(hue) 정합 / C 텍스트 배경 대비 WCAG 하한(대형 3:1, 그 외 4.5:1)
Writes <book_dir>/gate-report.json. On PASS copies draft/book.pdf -> final/<slug>.pdf.
Exit 0 = PASS, 1 = FAIL. (G6 visual judgement is the agent's job on the contact sheet.)
"""
import json, math, re, shutil, sys, unicodedata
from collections import Counter
from numbers import Real
from pathlib import Path
from statistics import median

try:  # PyMuPDF 1.24+ 신 모듈명, 구버전은 fitz만 제공
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pagemetrics import analyze  # noqa: E402

SKILL = Path(__file__).resolve().parent.parent
TOL = 1.5  # pt
MM2PT = 72 / 25.4

TAIL_HARD = {"essay": 0.35, "magazine": 0.35}          # default 0.45
TAIL_WARN = {"practical": 0.70, "insight": 0.70, "academic": 0.70,
             "business": 0.65, "essay": 0.55, "magazine": 0.50}
WARN_REPORT_ONLY = {"essay", "magazine", "insight"}    # 상용 꼬리 실측 표본 0인 스타일 — HARD만 강제
MID_HARD = 0.75
MID_ROLE_MIN = {"essay": 0.88, "insight": 0.85}       # default 0.90 — insight는 130mm 통짜 블록+H2 24mm 이월 물리
DOC_STATS_STYLES = {"practical", "academic"}  # 문서 통계는 실측 근거 있는 스타일만
ROLE_CODES = {"PART_DIVIDER", "FULL_BLEED_PLATE", "EXEC_SUMMARY",
              "ESSAY_BREATH", "MAGAZINE_WHITESPACE", "TOC_TAIL",
              "CH_CLOSE_APPROVED"}  # 꼬리 미달 최종 에스컬레이션 (pagination.md §4)


def norm(s):
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s​]+", "", s)
    s = re.sub(r"[\"'‘’“”「」『』*_`~]", "", s)
    return s.replace(",", "")


def g10_quote_check(book_dir, outline):
    """렌더 전 md 검사: pull 인용 분절(≥12자)과 stat/콜아웃 수치의 본문 실재."""
    problems = []
    for ch in outline["chapters"]:
        p = book_dir / "chapters" / ch["file"]
        if not p.exists():
            continue
        raw = p.read_text(encoding="utf-8")
        callouts = re.findall(r"^:::\s*(pull|statrow|stat|quote|info|tip|warn)[^\n]*\n(.*?)^:::\s*$",
                              raw, re.S | re.M)
        body = re.sub(r"^:::.*?^:::\s*$", "", raw, flags=re.S | re.M)
        nbody = norm(body)
        body_nums = set(re.findall(r"\d[\d,.]*", body.replace(",", "")))
        for kind, ctext in callouts:
            if kind == "pull":
                # build_html 계약과 동일: 1행 = 인용문, 2행 = 화자 라벨(검사 제외)
                ls = [l.strip() for l in ctext.split("\n") if l.strip()]
                quote_line = ls[0] if ls else ""
                frags = [f for f in re.split(r"[…⋯]|\.\.\.|[—–~]", quote_line) if len(norm(f)) >= 12]
                for f in frags:
                    if norm(f) not in nbody:
                        problems.append(f"{ch['file']}: pull 인용이 본문에 없음 — '{f.strip()[:40]}'")
            if kind in ("stat", "statrow"):
                for tok in re.findall(r"\d[\d,.]*", ctext.replace(",", "")):
                    if len(tok) < 2:  # 한 자리 토큰(5G·3nm류)은 오탐 — 검사 제외
                        continue
                    if tok in body_nums:
                        continue
                    ctx = ctext[max(0, ctext.find(tok) - 8):ctext.find(tok)]
                    approx = any(w in ctx for w in ("약", "가량", "내외", "여"))
                    if approx:
                        try:
                            v = float(tok)
                            if any(abs(float(b) - v) <= 0.05 * max(v, 1e-9)
                                   for b in body_nums if _isnum(b)):
                                continue
                        except ValueError:
                            pass
                    problems.append(f"{ch['file']}: stat 수치 '{tok}'가 본문에 없음")
    return problems


def _isnum(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def classify_bbox(raw_bbox, clip):
    """Return (status, normalized bbox) for a raw PyMuPDF block bbox."""
    if (not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4
            or any(isinstance(v, bool) or not isinstance(v, Real) for v in raw_bbox)):
        return "invalid", None
    try:
        coords = [float(v) for v in raw_bbox]
    except (TypeError, ValueError, OverflowError):
        return "invalid", None
    if not all(math.isfinite(v) for v in coords):
        return "invalid", None
    bbox = fitz.Rect(coords)
    bbox.normalize()
    if bbox.is_empty:
        return "empty", list(bbox)
    return ("inside" if clip.contains(bbox) else "overflow"), list(bbox)


def g3_check(doc):
    overflows, invalid_bboxes, empty_count = [], [], 0
    for pno in range(doc.page_count):
        page = doc[pno]
        pr = page.rect
        clip = fitz.Rect(pr.x0 - TOL, pr.y0 - TOL, pr.x1 + TOL, pr.y1 + TOL)
        for block in page.get_text("dict").get("blocks", []):
            raw_bbox = block.get("bbox")
            status, bbox = classify_bbox(raw_bbox, clip)
            item = {"page": pno + 1,
                    "kind": "text" if block.get("type") == 0 else "image"}
            if status == "overflow":
                overflows.append({**item, "bbox": bbox})
            elif status == "empty":
                empty_count += 1
            elif status == "invalid":
                invalid_bboxes.append({**item, "bbox": repr(raw_bbox)[:200]})
    return {"overflows": overflows[:20], "count": len(overflows),
            "invalid_bboxes": invalid_bboxes[:20], "invalid_count": len(invalid_bboxes),
            "empty_count": empty_count, "ok": not overflows and not invalid_bboxes}


IMG_REF_RE = re.compile(r'!\[[^\]]*\]\((\.\./assets/[^)"\s]+\.svg)(?:\s+"[^"]*")?\)')

# ---- G15 지면 리듬 (스타일별 실측 근거 있는 곳만 강제) ----
# PARA: 단락 최대 행수 (business STYLE.md T2 "단락 최대 8행") — 렌더 전 md 추정 검사
# RHYTHM: 시각 요소 없는 연속 본문 면 상한 (T2 "면당 시각 요소 1~2개"의 최소 방어선)
G15_PARA_CFG = {"business": {"max_lines": 8, "cpl": 36, "tol": 0.6}}
G15_DROUGHT_MAX = {"business": 3}


def _eff_chars(s):
    """행 길이 추정용 유효 글자수 — CJK 전각 1.0, 그 외 0.55."""
    return sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in s)


def g15_para_check(book_dir, outline, style):
    cfg = G15_PARA_CFG.get(style)
    if not cfg:
        return []
    problems = []
    for ch in outline["chapters"]:
        p = book_dir / "chapters" / ch["file"]
        if not p.exists():
            continue
        raw = re.sub(r"^:::.*?^:::\s*$", "", p.read_text(encoding="utf-8"), flags=re.S | re.M)
        for para in re.split(r"\n\s*\n", raw):
            para = para.strip()
            if not para or para[0] in "#!|>-+`[":
                continue  # 제목·이미지·표·인용·리스트·펜스는 비대상
            if re.match(r"\d+\.\s", para):
                continue  # 순번 리스트
            est = _eff_chars(para.replace("\n", "")) / cfg["cpl"]
            if est > cfg["max_lines"] + cfg["tol"]:
                problems.append(f"{ch['file']}: 단락 추정 {est:.1f}행 > {cfg['max_lines']}행 "
                                f"— '{para[:28]}…' 분할 필요")
    return problems


def g15_drought_check(pages, page_texts, first_ch, structural, style):
    """시각 요소(도해·표·박스·키 스탯 디스플레이) 없는 연속 본문 면 상한."""
    limit = G15_DROUGHT_MAX.get(style)
    if not limit:
        return []
    problems, run = [], []
    for p in pages:
        pg = p["page"]
        if pg < first_ch or pg in structural:
            if len(run) > limit:
                problems.append(f"연속 순텍스트 본문 {len(run)}면 {run} > {limit}면")
            run = []
            continue
        txt = page_texts[pg - 1]
        visual = (p["imgarea"] >= 0.02 or p.get("vecarea", 0) >= 0.02
                  or any(l["size"] >= 20 for l in p["_lines"])
                  or "<표" in txt or "[그림" in txt)
        if visual:
            if len(run) > limit:
                problems.append(f"연속 순텍스트 본문 {len(run)}면 {run} > {limit}면")
            run = []
        else:
            run.append(pg)
    if len(run) > limit:
        problems.append(f"연속 순텍스트 본문 {len(run)}면 {run} > {limit}면")
    return problems


def g0_svg_check(book_dir, outline):
    """렌더 전 도해 SVG 소스 검사 — Typst(usvg)는 foreignObject를 에러 없이 드롭하므로
    조용한 텍스트 전멸을 빌드 전에 차단한다."""
    problems = []
    referenced = []
    for ch in outline["chapters"]:
        p = book_dir / "chapters" / ch["file"]
        if not p.exists():
            continue
        paragraphs = re.split(r"\n\s*\n", p.read_text(encoding="utf-8"))
        for para in paragraphs:
            refs = IMG_REF_RE.findall(para)
            if not refs:
                continue
            # md2typ 승격 조건: 이미지 단독 문단이 아니면 이미지가 조용히 증발한다
            rest = IMG_REF_RE.sub("", para).strip()
            if rest:
                problems.append(f"{ch['file']}: SVG 이미지 문단에 다른 텍스트 혼합 — "
                                f"단독 문단이어야 승격됨: '{para.strip()[:50]}…'")
            referenced.extend(refs)
    for ref in referenced:
        name = ref[len("../assets/"):]
        svg_path = book_dir / "assets" / name
        if not svg_path.exists():
            problems.append(f"G0: 참조된 {name} 부재 (프리렌더 미실행?)")
            continue
        svg = svg_path.read_text(encoding="utf-8")
        if "<foreignObject" in svg:
            problems.append(f"{name}: foreignObject 잔존 — Typst에서 텍스트 전멸")
        if "xml-stylesheet" in svg or re.search(r'(?:href|src)="https?://', svg):
            problems.append(f"{name}: 외부 참조(CDN 폰트/미인라인 자원) 잔존")
        stem = name[:-len(".svg")]
        sidecar = book_dir / "diagrams" / f"{stem}.json"
        labels_path = book_dir / "assets" / f"{stem}.labels.json"
        if sidecar.exists():
            sc = json.loads(sidecar.read_text(encoding="utf-8"))
            bf = sc.get("bf", {})
            kind = sc.get("kind", "antv")
            if kind not in ("antv", "authored"):
                problems.append(f"{stem}.json: kind '{kind}'는 antv|authored만")
            if kind == "authored" and not (book_dir / "diagrams" / f"{stem}.svg").exists():
                problems.append(f"{stem}: kind=authored인데 diagrams/{stem}.svg 소스 부재")
            if not labels_path.exists():
                problems.append(f"{name}: labels.json 부재 — 프리렌더 산출물 불완전")
            elif json.loads(labels_path.read_text(encoding="utf-8")) and "<text" not in svg:
                problems.append(f"{name}: 라벨이 있는데 SVG에 <text> 0개")
            if bf.get("icons") is True and "<symbol" not in svg:
                problems.append(f"{name}: icons:true인데 <symbol> 0개 — 아이콘 조용한 탈락")
        elif "<text" not in svg and "<foreignObject" not in svg:
            # 수동 SVG(사이드카 없음)는 외부참조/foreignObject 검사만 — 텍스트 없는 순수 도형 허용
            pass
    return problems


def g13_figtext_check(book_dir, outline, page_texts):
    """렌더 후: 프리렌더 라벨(렌더된 줄 단위)이 PDF 실텍스트에 존재하는지 대조."""
    problems = []
    all_norm = norm("".join(page_texts))
    referenced = set()
    for ch in outline["chapters"]:
        p = book_dir / "chapters" / ch["file"]
        if p.exists():
            referenced.update(IMG_REF_RE.findall(p.read_text(encoding="utf-8")))
    for ref in sorted(referenced):
        stem = ref[len("../assets/"):-len(".svg")]
        labels_path = book_dir / "assets" / f"{stem}.labels.json"
        if not labels_path.exists():
            continue  # 수동 SVG — G13 비대상 (G0에서 소스 검사만)
        for label in json.loads(labels_path.read_text(encoding="utf-8")):
            if len(norm(label)) >= 2 and norm(label) not in all_norm:
                problems.append(f"{stem}: 라벨 '{label[:30]}'이 PDF 텍스트에 없음")
    return problems


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 scripts/qc_gate.py <book_dir> [--strict-pages]")
    book_dir = Path(sys.argv[1]).resolve()
    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    outline = json.loads((book_dir / "outline.json").read_text(encoding="utf-8"))
    style = book["style"]
    tokens = json.loads((SKILL / "styles" / style / "tokens.json").read_text(encoding="utf-8"))
    # metrics는 조기 실패에서도 항상 존재 (소비자가 키 존재를 가정할 수 있게)
    report = {"gates": {}, "warns": [], "pass": False, "metrics": {}}
    fails = []

    # ---- G10 (렌더 전 — 날조는 빌드보다 먼저 잡는다) ----
    g10 = g10_quote_check(book_dir, outline)
    report["gates"]["G10"] = {"problems": g10, "ok": not g10}
    if g10:
        finish(book_dir, report, ["G10: " + p for p in g10])

    # ---- G0 (렌더 전 — 도해 SVG 소스: usvg의 조용한 드롭은 빌드보다 먼저 잡는다) ----
    g0 = g0_svg_check(book_dir, outline)
    report["gates"]["G0"] = {"problems": g0, "ok": not g0}
    if g0:
        finish(book_dir, report, ["G0: " + p for p in g0])

    # ---- G15-PARA (렌더 전 — 단락 8행 초과는 원고 문제, 빌드보다 먼저 잡는다) ----
    g15p = g15_para_check(book_dir, outline, style)
    report["gates"]["G15-PARA"] = {"problems": g15p, "ok": not g15p}
    if g15p:
        finish(book_dir, report, ["G15-PARA: " + p for p in g15p])

    pdf = book_dir / "draft" / "book.pdf"

    # ---- G1 render + page count ----
    g1 = {"exists": pdf.exists()}
    if not pdf.exists():
        finish(book_dir, report, ["G1: draft/book.pdf missing"])
    doc = fitz.open(pdf)
    n = doc.page_count
    lo, hi = tokens.get("length_pages", {}).get(book.get("length", "short"), [10, 400])
    in_range = lo <= n <= hi
    # trim_mm(tokens) ↔ PDF 물리 크기 대조 — 테마 하드코딩이 tokens와 어긋나면 즉시 검출
    trim = tokens.get("trim_mm")
    trim_ok = True
    if trim:
        pr = doc[0].rect
        w_mm, h_mm = pr.width * 25.4 / 72, pr.height * 25.4 / 72
        trim_ok = abs(w_mm - trim[0]) <= 0.5 and abs(h_mm - trim[1]) <= 0.5
        if not trim_ok:
            fails.append(f"G1: 판형 불일치 — PDF {w_mm:.1f}×{h_mm:.1f}mm vs tokens trim_mm {trim} "
                         "(테마 하드코딩과 tokens가 갈라짐)")
        g1["trim_mm_measured"] = [round(w_mm, 1), round(h_mm, 1)]
        g1["trim_mm_expected"] = list(trim)
    g1["trim_ok"] = trim_ok
    # INV-1(pagination.md): 목표 쪽수는 조판의 입력이 아니다 — 산출물 쪽수는 기본
    # WARN. 하드 FAIL은 --strict-pages 명시 opt-in에서만 (강제 채움/개면 유인 차단).
    # ok는 이 게이트가 fails에 넣은 모든 사유(판형 포함)를 반영해야 진단이 안 갈라진다.
    strict_pages = "--strict-pages" in sys.argv[2:]
    g1.update({"pages": n, "range": [lo, hi],
               "ok": trim_ok and (in_range or not strict_pages),
               "strict": strict_pages})
    report["gates"]["G1"] = g1
    if not in_range:
        msg = f"G1: page count {n} outside [{lo},{hi}]"
        if strict_pages:
            fails.append(msg)
        else:
            report["warns"].append(msg + " (WARN — 쪽수는 조판을 압박하지 않는다, INV-1)")

    # ---- G2 font embedding ----
    # Type3 = Chromium이 CFF(.otf)를 서브셋 못해 글리프를 페이지별 벡터로 그린 것.
    # "임베드된 폰트"가 아니라 텍스트 추출·검색·접근성이 깨진 상태이므로 FAIL.
    # 대응: 폰트를 TrueType으로(scripts/convert_fonts.py), .otf를 @font-face에 쓰지 말 것.
    not_embedded = set()
    type3_pages = []
    for pno in range(n):
        for f in doc.get_page_fonts(pno, full=True):
            xref, ext, ftype, basefont = f[0], f[1], f[2], f[3]
            if ftype == "Type3":
                type3_pages.append(pno + 1)
                continue
            if ext in ("n/a", ""):
                extracted = doc.extract_font(xref)
                if not extracted or not extracted[-1]:
                    not_embedded.add(basefont)
    type3_pages = sorted(set(type3_pages))
    report["gates"]["G2"] = {"not_embedded": sorted(not_embedded),
                             "type3_pages": type3_pages,
                             "ok": not not_embedded and not type3_pages}
    if not_embedded:
        fails.append(f"G2: fonts not embedded: {sorted(not_embedded)}")
    if type3_pages:
        fails.append(f"G2: Type3 글리프 페이지 {type3_pages[:8]}{'…' if len(type3_pages) > 8 else ''} "
                     f"— CFF(.otf) @font-face가 원인, TTF로 교체할 것 (convert_fonts.py)")

    # ---- G3 overflow ----
    g3 = g3_check(doc)
    report["gates"]["G3"] = g3
    if g3["count"]:
        fails.append(f"G3: {g3['count']} bbox overflow(s), first on page {g3['overflows'][0]['page']}")
    if g3["invalid_count"]:
        fails.append(f"G3: {g3['invalid_count']} invalid bbox(es), "
                     f"first on page {g3['invalid_bboxes'][0]['page']}")

    # ---- G4 TOC / bookmarks ----
    toc_entries = doc.get_toc(simple=True)
    lvl1 = [(t.strip(), p) for (l, t, p) in toc_entries if l == 1]
    g4 = {"bookmarks": len(lvl1), "mismatches": [], "ok": True}
    want = [ch["title"].strip() for ch in outline["chapters"]]
    if len(lvl1) < len(want):
        g4["ok"] = False
        g4["mismatches"].append(f"bookmark count {len(lvl1)} < chapters {len(want)}")
    else:
        for title, page in lvl1:
            if page < 1 or page > n:
                g4["ok"] = False
                g4["mismatches"].append(f"'{title}' -> bad page {page}")
            else:
                text = doc[page - 1].get_text()
                if norm(title) not in norm(text):
                    g4["ok"] = False
                    g4["mismatches"].append(f"'{title}' not found on its page {page}")
    report["gates"]["G4"] = g4
    if not g4["ok"]:
        fails.append("G4: " + "; ".join(g4["mismatches"][:3]))

    page_texts = [doc[i].get_text() for i in range(n)]
    doc.close()

    # ---- G13 figtext (도해 라벨의 PDF 실텍스트 실재 — G11 anchor와 동일 패턴) ----
    g13 = g13_figtext_check(book_dir, outline, page_texts)
    report["gates"]["G13"] = {"problems": g13, "ok": not g13}
    if g13:
        fails.append("G13: " + "; ".join(g13[:3]) + (f" 외 {len(g13)-3}건" if len(g13) > 3 else ""))

    # ---- 밀도 전처리 (pagination.md §7) ----
    frame_mm = tokens.get("body_frame_mm")
    if not frame_mm:
        finish(book_dir, report, [f"G7: styles/{style}/tokens.json에 body_frame_mm 없음"])
    m = analyze(pdf, frame_mm)
    pages = m["pages"]
    N = m["n_grid"] or 1

    ch_starts = sorted({p for (_, p) in lvl1 if 1 <= p <= n})
    first_ch = ch_starts[0] if ch_starts else 1

    # ---- G14 목차·디자인 정합 (tocgate.py) ----
    from tocgate import run as g14_run
    g14_doc = fitz.open(pdf)  # 본 doc은 page_texts 추출 후 닫혔음
    g14 = g14_run(g14_doc, outline, ch_starts, book, tokens)
    g14_doc.close()
    report["gates"]["G14-A"] = g14["A"]
    report["gates"]["G14-B"] = g14["B"]
    report["gates"]["G14-C"] = g14["C"]
    for axis in ("A", "B", "C"):
        for p in g14[axis]["problems"]:
            fails.append(f"G14-{axis}: {p}")
    colophon_pages = {i + 1 for i, t in enumerate(page_texts)
                      if "bookforge" in t and "조판" in t and i + 1 >= (ch_starts[-1] if ch_starts else 1)}
    fullbleed = {p["page"] for p in pages
                 if p["imgarea"] >= 0.60 or p.get("vecarea", 0) >= 0.60}
    # float 밀림 면제(구조 파생): 다음 면 첫 블록(통짜 표·그림)이 이 면 잔여 공간보다 크면
    # 이 면의 미달은 결속 규칙의 정당한 대가다 — 중간면 판정에서 제외.
    float_pushed = set()
    pitch_ref = m["book_pitch"] or 12
    for i, p in enumerate(pages[:-1]):
        nxt = pages[i + 1]
        fl, ft, fr, fb = nxt["frame"]
        # 텍스트는 나눠 흐를 수 있으므로 면제 사유가 못 된다 — 객체(표 괘선·그림·박스)만 본다.
        segs = sorted(nxt["_objs"])
        if not segs or segs[0][0] > ft + 2 * pitch_ref:
            continue  # 다음 면이 객체로 시작하지 않으면 밀림이 아니다
        top0, cur1 = segs[0]
        for a, b in segs[1:]:
            if a - cur1 > pitch_ref * 3:  # 표는 모든 행에 괘선이 있지 않다 — 행 건너뜀 허용
                break
            cur1 = max(cur1, b)
        first_block_h = cur1 - top0
        remaining = (1 - p["reach"]) * (p["frame"][3] - p["frame"][1])
        # 블록은 CSS/Typst 마진을 데리고 다닌다 — 3행송 슬랙 인정
        if first_block_h + pitch_ref * 3 > remaining:
            float_pushed.add(p["page"])
    body_last = max((p["page"] for p in pages
                     if p["lines"] > 0 and p["page"] not in colophon_pages), default=n)
    # 마지막 본문 면은 구조 파생 면제(pagination.md §7 — 책의 끝은 자연 꼬리)
    tails = {p - 1 for p in ch_starts if p - 1 >= first_ch}
    structural = (set(range(1, first_ch)) | set(ch_starts) | colophon_pages | fullbleed
                  | {body_last})  # 마지막 본문 면 면제 (pagination.md §7)

    # ---- G15-RHYTHM 시각 요소 없는 연속 본문 면 (스타일별 상한) ----
    g15r = g15_drought_check(pages, page_texts, first_ch, structural, style)
    report["gates"]["G15-RHYTHM"] = {"problems": g15r, "ok": not g15r}
    if g15r:
        fails.append("G15-RHYTHM: " + "; ".join(g15r[:3]))

    # ---- G11 pageroles.json ----
    roles_p = book_dir / "pageroles.json"
    roles = []
    g11 = {"declared": 0, "problems": [], "ok": True}
    if roles_p.exists():
        roles = json.loads(roles_p.read_text(encoding="utf-8")).get("roles", [])
        g11["declared"] = len(roles)
        budget = max(3, int(0.08 * n))
        if len(roles) > budget:
            g11["problems"].append(f"선언 면 {len(roles)} > 예산 {budget}")
        breaths = 0
        for r in roles:
            pg, code = r.get("page"), r.get("code")
            why, anchor = (r.get("why") or "").strip(), r.get("anchor")
            pm = next((p for p in pages if p["page"] == pg), None)
            if code not in ROLE_CODES:
                g11["problems"].append(f"p{pg}: 코드 '{code}' 화이트리스트 밖"); continue
            if not why:
                g11["problems"].append(f"p{pg}: why 비어 있음")
            if pm is None or not (1 <= pg <= n):
                g11["problems"].append(f"p{pg}: 존재하지 않는 면"); continue
            # 기계 선행조건 — 불충족 시 코드 자체가 FAIL (도장 방지)
            if code == "FULL_BLEED_PLATE" and pm["imgarea"] < 0.60:
                g11["problems"].append(f"p{pg}: FULL_BLEED_PLATE인데 imgarea {pm['imgarea']} < 0.60")
            if code == "PART_DIVIDER" and pm["lines"] > 3:
                g11["problems"].append(f"p{pg}: PART_DIVIDER인데 텍스트 {pm['lines']}행 > 3")
            if code == "EXEC_SUMMARY":
                if style != "business" or not (0.35 <= pm["ink"] <= 0.70):
                    g11["problems"].append(f"p{pg}: EXEC_SUMMARY 선행조건 위반 (style={style}, ink={pm['ink']})")
            if code == "ESSAY_BREATH":
                breaths += 1
                if style != "essay" or pg not in tails or pm["lines"] < 6:
                    g11["problems"].append(f"p{pg}: ESSAY_BREATH 선행조건 위반 (꼬리 면·6행 이상·essay 한정)")
            if code == "MAGAZINE_WHITESPACE" and style != "magazine":
                g11["problems"].append(f"p{pg}: MAGAZINE_WHITESPACE는 magazine 한정")
            if code == "TOC_TAIL" and pg >= first_ch:
                g11["problems"].append(f"p{pg}: TOC_TAIL은 본문 시작 전 한정")
            if code == "CH_CLOSE_APPROVED" and pg not in tails:
                g11["problems"].append(f"p{pg}: CH_CLOSE_APPROVED는 장 끝 면 한정 "
                                       "(레버 소진 후 최종 에스컬레이션)")
            if anchor:
                if norm(anchor) not in norm(page_texts[pg - 1]):
                    g11["problems"].append(f"p{pg}: anchor 불일치(stale — 리빌드로 면 밀림 의심)")
            elif code not in ("FULL_BLEED_PLATE", "PART_DIVIDER"):
                g11["problems"].append(f"p{pg}: anchor 필수(텍스트 면)")
        if breaths > max(1, len(ch_starts)):
            g11["problems"].append(f"ESSAY_BREATH {breaths}회 > 장당 1회 한도")
    g11["ok"] = not g11["problems"]
    report["gates"]["G11"] = g11
    if not g11["ok"]:
        fails.append("G11: " + "; ".join(g11["problems"][:3]))
    role_by_page = {r.get("page"): r.get("code") for r in roles}

    # ---- G7-FRAME 판면 드리프트 ----
    ft_pt = frame_mm[0] * MM2PT
    d_top = m["derived_frame"][0]
    g7f = {"token_top_pt": round(ft_pt, 1), "derived_top_pt": d_top,
           "book_pitch": m["book_pitch"], "n_grid": N, "ok": True}
    if d_top is not None and abs(d_top - ft_pt) > 6:
        g7f["ok"] = False
        fails.append(f"G7-FRAME: tokens 판면 상단 {ft_pt:.1f}pt vs 실측 {d_top}pt — 드리프트 >6pt")
    report["gates"]["G7-FRAME"] = g7f

    # ---- G7-BLANK (구 G5 흡수) + G12 ----
    blanks, parity = [], []
    for p in pages:
        pg = p["page"]
        if p["ink"] >= 0.03 or pg in structural or pg in role_by_page:
            continue
        if pg + 1 in ch_starts:
            parity.append(pg)  # 장 시작 직전 필러 백면 — 인쇄 관습 이식
        else:
            blanks.append(pg)
    report["gates"]["G7-BLANK"] = {"blank_pages": blanks, "ok": not blanks}
    report["gates"]["G12"] = {"parity_filler": parity, "ok": not parity}
    if blanks:
        fails.append(f"G7-BLANK: 의도 없는 백면 {blanks}")
    if parity:
        fails.append(f"G12: 장 시작 직전 필러 백면 {parity} (단면 전자책에 recto 맞춤 금지)")

    # ---- G7-TAIL / G7-MID ----
    tail_hard = TAIL_HARD.get(style, 0.45)
    tail_warn = TAIL_WARN.get(style, 0.70)
    mid_role_min = MID_ROLE_MIN.get(style, 0.90)
    g7t = {"tails": [], "ok": True}
    g7m = {"underfull": [], "ok": True}
    tail_reaches = []
    for p in pages:
        pg = p["page"]
        if (pg < first_ch or pg in colophon_pages or pg in fullbleed or pg in ch_starts
                or pg == body_last):  # 마지막 본문 면 면제 (pagination.md §7)
            continue
        code = role_by_page.get(pg)
        if pg in tails:
            tail_reaches.append(p["reach"])
            entry = {"page": pg, "reach": p["reach"], "lines": p["lines"], "role": code}
            g7t["tails"].append(entry)
            if p["lines"] < 6:
                g7t["ok"] = False
                fails.append(f"G7-TAIL: p{pg} 꼬리 {p['lines']}행 < 6 (HARD — 사유 코드 불가)")
            elif p["reach"] < tail_hard:
                g7t["ok"] = False
                fails.append(f"G7-TAIL: p{pg} reach {p['reach']} < HARD {tail_hard}")
            elif p["reach"] < tail_warn and not code:
                if style in WARN_REPORT_ONLY:
                    report["warns"].append(f"G7-TAIL: p{pg} reach {p['reach']} < {tail_warn} (report-only)")
                else:
                    g7t["ok"] = False
                    fails.append(f"G7-TAIL: p{pg} reach {p['reach']} < {tail_warn}, 사유 코드 없음")
        else:
            if pg in float_pushed:
                report["warns"].append(f"G7-MID: p{pg} reach {p['reach']} — float 밀림 면제(다음 면 통짜 블록)")
                continue
            if p["reach"] < MID_HARD:
                g7m["underfull"].append({"page": pg, "reach": p["reach"]})
                g7m["ok"] = False
                fails.append(f"G7-MID: p{pg} reach {p['reach']} < {MID_HARD} (HARD)")
            elif p["reach"] < mid_role_min and not code:
                if style in WARN_REPORT_ONLY:
                    report["warns"].append(f"G7-MID: p{pg} reach {p['reach']} < {mid_role_min} (report-only)")
                else:
                    g7m["underfull"].append({"page": pg, "reach": p["reach"]})
                    g7m["ok"] = False
                    fails.append(f"G7-MID: p{pg} reach {p['reach']} < {mid_role_min}, 사유 코드 없음")
    report["gates"]["G7-TAIL"] = g7t
    report["gates"]["G7-MID"] = g7m

    # ---- G7-DOC 문서 통계 (실측 근거 있는 스타일만) ----
    if style in DOC_STATS_STYLES and tail_reaches:
        med = round(median(tail_reaches), 3)
        p10 = round(sorted(tail_reaches)[max(0, int(0.1 * len(tail_reaches)) - 0)], 3) \
            if len(tail_reaches) >= 3 else min(tail_reaches)
        ok = med >= 0.80 and p10 >= 0.55
        report["gates"]["G7-DOC"] = {"median": med, "p10": p10, "ok": ok}
        if not ok:
            fails.append(f"G7-DOC: 꼬리 reach 중앙값 {med}/p10 {p10} < 0.80/0.55 — 원고 분량 설계 반환")

    # 본문 크기 = 글자 수 가중 최빈값(행 수 기준이면 리스트·표 9pt가 본문 10.5pt를 이길 수 있다)
    sizes = Counter()
    for p in pages:
        for l in p["_lines"]:
            sizes[round(l["size"] * 2) / 2] += len(l["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 10.0

    # ---- G8-STRETCH 공기 채움 ----
    g8 = {"stretched": [], "ok": True}
    for p in pages:
        pg = p["page"]
        if pg < first_ch or pg in structural or pg in tails or pg in role_by_page \
                or pg in float_pushed:
            continue
        # insight는 H2 위 여백 24mm(STYLE.md 정본)+와이드 콜아웃이 구조적 공기를 만든다 — 임계 완화.
        # 한 면에 섹션 전환이 2회 이상이면 여백이 배로 쌓이므로 디스플레이 행 수 비례 가산.
        gap_thr = 0.18
        if style == "insight":
            heads = sum(1 for l in p["_lines"] if l["size"] >= 1.3 * body_size)
            gap_thr = 0.28 + 0.10 * max(0, heads - 1)
        elif style == "business":
            # 액션 타이틀(16pt + 전폭 룰 + 전후 v1.8/1.1em)과 키 스탯 디스플레이가
            # 면당 ~10mm(≈0.045)의 구조적 공기를 만든다(실측 — p13 홀 스캔). 디스플레이
            # 행 수에 비례해 가산, 디스플레이 없는 순본문 면은 0.18 그대로.
            heads = sum(1 for l in p["_lines"] if l["size"] >= 1.3 * body_size)
            gap_thr = 0.18 + 0.05 * heads
        if p["gap"] > gap_thr and p["lines"] < 0.8 * N:
            g8["stretched"].append({"page": pg, "gap": p["gap"], "lines": p["lines"]})
        # 행송 편차는 WARN만 — 두 엔진 모두 페이지 단위로 행송을 벌릴 능력이 없다(실측).
        # 리스트·코드·콜아웃 혼합 면의 자연 편차가 대부분이라 FAIL로 쓰면 오탐.
        if p["pitch"] and m["book_pitch"] and p["lines"] >= 10 and \
                abs(p["pitch"] - m["book_pitch"]) / m["book_pitch"] > 0.03:
            report["warns"].append(f"G8: p{pg} 행송 편차 {p['pitch']} vs {m['book_pitch']} (혼합 콘텐츠 추정)")
    g8["ok"] = not g8["stretched"]
    report["gates"]["G8"] = g8
    if g8["stretched"]:
        first = g8["stretched"][0]
        fails.append(f"G8-STRETCH: 공기 채움/행송 이탈 {len(g8['stretched'])}면, 첫 면 p{first['page']}")

    # ---- G9-KEEP 제목 고립·widow ----
    g9 = {"violations": [], "ok": True}
    single_col = style in ("practical", "academic", "essay", "business")
    for i, p in enumerate(pages):
        pg = p["page"]
        if pg < first_ch or pg in structural or not p["_lines"]:
            continue
        last = p["_lines"][-1]
        if last["size"] >= 1.3 * body_size and pg not in tails:  # 1.1~1.3 대역은 스탯 라벨·덱 오탐(폰트 판별은 백로그)
            g9["violations"].append(f"p{pg}: 면 끝 제목 고립('{last['text'][:20]}')")
        if single_col and i > 0 and pages[i - 1]["_lines"] and pg - 1 >= first_ch \
                and pg - 1 not in ch_starts:
            fl_, _, fr_, _ = p["frame"]
            first_l, prev_last = p["_lines"][0], pages[i - 1]["_lines"][-1]
            w = fr_ - fl_
            if abs(first_l["size"] - body_size) <= 0.5 and \
                    first_l["x1"] < fr_ - 0.15 * w and prev_last["x1"] >= fr_ - 0.05 * w and \
                    len(p["_lines"]) >= 2 and abs(p["_lines"][1]["size"] - body_size) <= 0.5 and \
                    p["_lines"][1]["y0"] - first_l["y0"] > (m["book_pitch"] or 12) * 1.5:
                g9["violations"].append(f"p{pg}: widow 의심(면 첫 행이 앞 문단 끝줄)")
    g9["ok"] = not g9["violations"]
    report["gates"]["G9"] = g9
    if g9["violations"]:
        fails.append("G9-KEEP: " + "; ".join(g9["violations"][:3]))

    # metrics 요약을 리포트에 (디버그·refit 입력)
    report["metrics"] = {"book_pitch": m["book_pitch"], "n_grid": N,
                         "pages": [{k: p[k] for k in ("page", "lines", "reach", "ink", "gap", "imgarea")}
                                   for p in pages],
                         "chapter_starts": ch_starts, "tails": sorted(tails),
                         "structural_exempt": sorted(structural)}

    if fails:
        finish(book_dir, report, fails)

    report["pass"] = True
    final_dir = book_dir / "final"
    final_dir.mkdir(exist_ok=True)
    dst = final_dir / f"{book_dir.name}.pdf"
    shutil.copy(pdf, dst)
    report["final"] = str(dst)
    (book_dir / "gate-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for w in report["warns"]:
        print("WARN", w)
    print(f"PASS -> {dst}")
    sys.exit(0)


def finish(book_dir, report, fails):
    report["fails"] = fails
    # FAIL이면 이전 회차의 final/을 무효화한다 — 게이트를 통과하지 못한 시점의
    # 스테일 PDF가 final/에 남아 "완료"로 오판되는 것을 차단 (SKILL.md의 final 계약).
    stale = book_dir / "final" / f"{book_dir.name}.pdf"
    if stale.exists():
        stale.unlink()
        print(f"FAIL -> 스테일 final 제거: {stale}", file=sys.stderr)
    (book_dir / "gate-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for w in report.get("warns", []):
        print("WARN", w, file=sys.stderr)
    for f in fails:
        print("FAIL", f, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
