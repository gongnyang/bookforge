"""한글 어절 단위 줄바꿈(book-anatomy.md A.1) 회귀 테스트.

python3 -m unittest tests.test_keep_all
조판 테스트는 typst 0.14가 PATH에 있을 때만 돈다(없으면 skip).
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from keep_all import WJ, insert_wj, residual_pages, strip_pdf  # noqa: E402
import md2typ  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz


def typst_014():
    exe = shutil.which("typst")
    if not exe:
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    return exe if re.search(r"\b0\.14\.", out) else None


class InsertTests(unittest.TestCase):
    def mark(self, s):
        return insert_wj(s).replace(WJ, "|")

    def test_joins_inside_korean_eojeol(self):
        self.assertEqual(self.mark("법령 MCP는 4.15.9판"), "법|령 MCP|는 4.15.9|판")
        self.assertEqual(self.mark("(관심 표시)"), "(|관|심 표|시|)")

    def test_leaves_latin_runs_to_uax14(self):
        # URL·하이픈 영문 낱말은 줄바꿈 기회를 그대로 둔다(한글에 닿는 자리만 묶는다)
        self.assertEqual(self.mark("github.com/gongnyang"), "github.com/gongnyang")
        self.assertEqual(self.mark("Qwen2.5-7B-Instruct의"), "Qwen2.5-7B-Instruct|의")

    def test_spaces_and_middle_dot(self):
        self.assertEqual(self.mark("10월 26일"), "10|월 26|일")
        # 가운뎃점 앞은 묶고(줄 첫머리 금지) 뒤는 비운다(긴 나열이 갈릴 자리)
        self.assertEqual(self.mark("경제·매일"), "경|제|·매|일")

    def test_idempotent(self):
        once = insert_wj("어절 단위 줄바꿈")
        self.assertEqual(insert_wj(once), once)


class Md2TypTests(unittest.TestCase):
    MD = ("# 장 제목\n\n본문 문단은 어절로 묶는다.\n\n"
          "[표] 캡션도 묶는다 | 자료: 출처\n\n| 머리 | 칸 |\n|---|---|\n| 표 칸은 | 묶지 않는다 |\n")

    def convert(self, keep_all):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "ch.md", Path(d) / "ch.typ"
            src.write_text(self.MD, encoding="utf-8")
            md2typ.convert_chapter(src, dst, "장 제목", "요약 문장", keep_all=keep_all)
            return dst.read_text(encoding="utf-8")

    def test_body_caption_summary_joined_cells_untouched(self):
        out = self.convert(True)
        self.assertIn("justification-limits", out)
        self.assertIn(insert_wj("본문 문단은"), out)
        self.assertIn(insert_wj("캡션도 묶는다"), out)
        self.assertIn(insert_wj("요약 문장"), out)
        # 표 칸은 A.1 좁은 단 예외 — 음절 단위 그대로
        self.assertIn("[표 칸은]", out)
        self.assertIn("[묶지 않는다]", out)
        # 장 제목 문자열(북마크·러닝 푸터로 가는 값)은 건드리지 않는다
        self.assertIn('#bf-chapter("장 제목"', out)

    def test_off_is_unchanged(self):
        out = self.convert(False)
        self.assertNotIn(WJ, out)
        self.assertNotIn("justification-limits", out)


@unittest.skipUnless(typst_014(), "typst 0.14가 PATH에 없다")
class TypesetTests(unittest.TestCase):
    TEXT = ("공공데이터 도구 네 가지를 새로 만들었고 주요 도구 세 가지는 새 판을 359번 냈다. "
            "7월 스마트정보과로 옮긴 뒤에는 도구를 광진GPT와 행정포털에 올려 직원 업무에 쓰게 했다. "
            "광진구는 이 사례로 제43회 지역정보화 우수사례 발표대회에서 국무총리상을 받았다.")

    def test_breaks_only_at_spaces_and_text_layer_clean(self):
        with tempfile.TemporaryDirectory() as d:
            typ, pdf = Path(d) / "t.typ", Path(d) / "t.pdf"
            typ.write_text(
                '#set page(width: 100mm, height: 120mm, margin: 8mm)\n'
                '#set text(font: "Noto Serif KR", size: 10pt, lang: "ko")\n'
                + md2typ.KEEP_ALL_PRELUDE + "#set par(justify: true)\n"
                + md2typ.esc(insert_wj(self.TEXT)) + "\n", encoding="utf-8")
            subprocess.run([typst_014(), "compile", "--font-path", str(ROOT / "assets" / "fonts"),
                            "--ignore-system-fonts", str(typ), str(pdf)], check=True)
            strip_pdf(pdf)
            self.assertEqual(residual_pages(pdf), [])
            page = fitz.open(pdf)[0]
            self.assertEqual(len(page.search_for("국무총리상")), 1)
            lines = [ln.strip() for ln in page.get_text().split("\n") if ln.strip()]
            self.assertGreater(len(lines), 3)
            # 줄 끝 어절 + 다음 줄 첫 어절이 원문에서 띄어쓰기로 갈려 있어야 한다
            for a, b in zip(lines, lines[1:]):
                self.assertIn(a.split()[-1] + " " + b.split()[0], self.TEXT, (a, b))


if __name__ == "__main__":
    unittest.main()
