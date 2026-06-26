#!/usr/bin/env python3
"""Build puzzles/index.html — a simple menu linking to every generated puzzle.

Scans the puzzles/ directory for *.html files (excluding index.html itself),
pulls each one's <title>, and writes a styled landing page. Deterministic and
dependency-free, so it is safe to run in CI before deploying to GitHub Pages.

    python3 crossword/build_index.py
"""

import html
import re
from pathlib import Path

PUZZLES = Path(__file__).resolve().parent.parent / "puzzles"

_PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deutsch-Kreuzworträtsel</title>
<style>
  body {{ margin:0; padding:32px 20px; background:#f4f1ea; color:#1c1c1c;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:640px; margin:0 auto; }}
  h1 {{ font-size:1.5rem; margin:0 0 4px; }}
  p.sub {{ color:#666; margin:0 0 24px; }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ margin:0 0 12px; }}
  a.card {{ display:block; text-decoration:none; color:inherit; background:#fff;
    border:1px solid #e0d6bd; border-radius:12px; padding:16px 18px;
    box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  a.card:hover {{ background:#fffbf0; border-color:#d9a90f; }}
  a.card .title {{ font-weight:600; font-size:1.1rem; }}
  a.card .go {{ float:right; color:#d9a90f; font-weight:700; }}
  .empty {{ color:#888; }}
  footer {{ margin-top:28px; color:#999; font-size:.85rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Deutsch-Kreuzworträtsel 🇩🇪</h1>
  <p class="sub">Tippe ein Rätsel an, um es zu lösen.</p>
  <ul>
{items}
  </ul>
  <footer>Tipp: „Zum Home-Bildschirm hinzufügen“, dann funktioniert es wie eine App.</footer>
</div>
</body>
</html>
"""


def extract_title(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"<title>(.*?)</title>", text, re.S)
    return html.unescape(m.group(1).strip()) if m else path.stem


def main():
    pages = sorted(p for p in PUZZLES.glob("*.html") if p.name != "index.html")
    if pages:
        items = "\n".join(
            f'    <li><a class="card" href="{html.escape(p.name)}">'
            f'<span class="go">&rsaquo;</span>'
            f'<span class="title">{html.escape(extract_title(p))}</span></a></li>'
            for p in pages
        )
    else:
        items = '    <li class="empty">Noch keine Rätsel.</li>'

    out = PUZZLES / "index.html"
    out.write_text(_PAGE.format(items=items), encoding="utf-8")
    print(f"Wrote {out} ({len(pages)} puzzle(s))")


if __name__ == "__main__":
    main()
