#!/usr/bin/env python3
"""bookforge: 한글 어절 단위 줄바꿈(references/book-anatomy.md A.1), Typst 트랙.

Typst 0.14는 한글을 음절마다 끊는다(UAX#14 ID 계열). CSS `word-break: keep-all`에 해당하는
설정이 없어서, 어절 안 글자 사이에 줄바꿈 금지 문자 U+2060(WORD JOINER)을 넣어 띄어쓰기에서만
줄이 바뀌게 한다(insert_wj: md2typ가 본문·제목·캡션 글에 적용). 양끝 맞춤은 A.3의 어간·자간
상한 안에서 Typst가 고르게 나눈다.

넣은 문자는 PDF 글자 층(ToUnicode·ActualText·북마크)에도 따라 들어가 검색·복사·게이트의 글자
추출을 깨뜨린다. 조판 뒤 strip_pdf가 글자 층에서만 지운다. 글리프 배치는 그대로다.
"""
import re
import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF < 1.24
    import fitz

WJ = "⁠"
# 한글(자모·호환 자모·음절)·가나·한자: 이 글자에 닿는 자리만 묶는다. 라틴·숫자 연속은
# UAX#14 그대로 두어 URL·영문 낱말·하이픈 뒤 줄바꿈 기회를 지킨다.
_CJK = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힣぀-ヿ㐀-䶿一-鿿]")


def insert_wj(text: str) -> str:
    """어절 안 글자 사이(한쪽이라도 한글·한자·가나인 자리)에 U+2060을 넣는다.
    띄어쓰기·줄바꿈 없는 공백 옆, 가운뎃점 뒤(「경제·매일경제」 같은 긴 나열)는 비운다."""
    out, prev = [], ""
    for ch in text:
        if (prev and not prev.isspace() and not ch.isspace() and prev != "·"
                and ch != WJ and prev != WJ and (_CJK.match(prev) or _CJK.match(ch))):
            out.append(WJ)
        out.append(ch)
        prev = ch
    return "".join(out)


def _clean_units(hexstr: bytes) -> bytes:
    """UTF-16BE 16진 문자열에서 2060 단위를 뺀다(4자리 정렬. 서로게이트 범위와 겹치지 않는다)."""
    h = re.sub(rb"\s+", b"", hexstr)
    units = [h[i:i + 4] for i in range(0, len(h), 4)]
    return b"".join(u for u in units if u.upper() != b"2060")


_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>")
_BFCHAR = re.compile(rb"(\d+)\s+beginbfchar(.*?)endbfchar", re.S)
_BFRANGE_ARR = re.compile(rb"\[([^\]]*)\]")
_ACTUAL_HEX = re.compile(rb"(/ActualText\s*)<([0-9A-Fa-f\s]*)>")


def _clean_cmap(data: bytes):
    n = 0

    def bfchar(m):
        nonlocal n
        pairs = []
        for src, dst in _PAIR.findall(m.group(2)):
            new = _clean_units(dst)
            n += new != dst
            if new:  # 2060만 뜻하던 항목은 버린다(글자 없음)
                pairs.append(b"<" + src + b"> <" + new + b">")
        return str(len(pairs)).encode() + b" beginbfchar\n" + b"\n".join(pairs) + b"\nendbfchar"

    data = _BFCHAR.sub(bfchar, data)

    def arr(m):
        nonlocal n
        body = m.group(1)
        new = re.sub(rb"<([0-9A-Fa-f]*)>", lambda t: b"<" + _clean_units(t.group(1)) + b">", body)
        n += new != body
        return b"[" + new + b"]"

    if b"beginbfrange" in data:
        data = re.sub(rb"beginbfrange(.*?)endbfrange",
                      lambda m: b"beginbfrange" + _BFRANGE_ARR.sub(arr, m.group(1)) + b"endbfrange",
                      data, flags=re.S)
    return data, n


def _clean_actualtext(data: bytes):
    n = 0

    def sub(m):
        nonlocal n
        h = re.sub(rb"\s+", b"", m.group(2))
        if not h.upper().startswith(b"FEFF") or b"2060" not in h.upper():
            return m.group(0)
        n += 1
        return m.group(1) + b"<FEFF" + _clean_units(h[4:]) + b">"

    return _ACTUAL_HEX.sub(sub, data), n


def strip_pdf(path) -> dict:
    """PDF 글자 층에서 U+2060을 지운다: 글꼴 ToUnicode CMap, 내용 스트림의 ActualText,
    구조·북마크의 문자열 키. 조판 결과(글리프·좌표)는 건드리지 않는다. 지운 수를 돌려준다."""
    path = Path(path)
    doc = fitz.open(path)
    stats = {"tounicode": 0, "actualtext": 0, "keys": 0}
    for x in range(1, doc.xref_length()):
        if doc.xref_is_stream(x):
            raw = doc.xref_stream(x)
            if not raw:
                continue
            new = raw
            if b"beginbfchar" in new or b"beginbfrange" in new:
                new, k = _clean_cmap(new)
                stats["tounicode"] += k
            if b"/ActualText" in new:
                new, k = _clean_actualtext(new)
                stats["actualtext"] += k
            if new != raw:
                doc.update_stream(x, new)
        # 북마크 제목·구조 요소의 ActualText/Alt 같은 문자열 키
        for key in ("Title", "ActualText", "Alt", "T"):
            try:
                kind, val = doc.xref_get_key(x, key)
            except Exception:
                continue
            if kind == "string" and WJ in val:
                doc.xref_set_key(x, key, fitz.get_pdf_str(val.replace(WJ, "")))
                stats["keys"] += 1
    tmp = path.with_suffix(".wj.tmp.pdf")
    doc.save(tmp, garbage=0, deflate=True)
    doc.close()
    tmp.replace(path)
    return stats


def residual_pages(path) -> list:
    """글자 추출에 U+2060이 남은 면 번호(1부터). 비어 있어야 한다."""
    doc = fitz.open(path)
    pages = [i + 1 for i, p in enumerate(doc) if WJ in p.get_text()]
    doc.close()
    return pages


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 scripts/keep_all.py <pdf>   # 글자 층에서 U+2060 지우기")
    print(strip_pdf(sys.argv[1]), "residual pages:", residual_pages(sys.argv[1]))
