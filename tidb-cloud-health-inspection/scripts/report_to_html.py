#!/usr/bin/env python3
"""Local-preview fallback: bundle a rendered health report (Markdown + its chart
PNGs) into ONE self-contained HTML file for quick viewing without any external
tool. Chart images are inlined as base64 data URIs and the Markdown is rendered
client-side with marked.js.

NOT the publishing path: Feishu/Lark publishing goes through the global `lark-cli`
as the user (markdown import embeds @./ local chart images natively). Use this only
to eyeball a report locally when lark-cli is unavailable.

Usage:
    report_to_html.py <report.md> <charts_dir> <out.html> [--title T]
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

_IMG_RE = re.compile(r"!\[([^\]]*)\]\((charts/[^)]+)\)")

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", Segoe UI, Roboto, sans-serif;
  max-width: 960px; margin: 0 auto; padding: 32px 16px 64px; line-height: 1.65;
  color: #1a1a1a; background: #fff; }
h1 { font-size: 1.7rem; border-bottom: 2px solid #eee; padding-bottom: .4em; }
h2 { font-size: 1.3rem; margin-top: 2em; border-bottom: 1px solid #eee; padding-bottom: .3em; }
h3 { font-size: 1.1rem; margin-top: 1.4em; color: #333; }
blockquote { color: #555; border-left: 4px solid #ddd; margin: 1em 0; padding: .3em 1em; background: #fafafa; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .92rem; }
th, td { border: 1px solid #e2e2e2; padding: 7px 10px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
tr:nth-child(even) td { background: #fbfbfb; }
img { max-width: 100%; height: auto; display: block; margin: 12px 0 20px; border: 1px solid #eee; border-radius: 6px; }
code { background: #f3f3f3; padding: 1px 5px; border-radius: 4px; font-size: .9em; }
em { color: #666; }
"""


def _inline_images(md: str, charts_dir: Path) -> str:
    def repl(m: re.Match) -> str:
        alt, rel = m.group(1), m.group(2)
        png = charts_dir / Path(rel).name
        if not png.exists():
            return m.group(0)
        data = base64.b64encode(png.read_bytes()).decode("ascii")
        return f"![{alt}](data:image/png;base64,{data})"
    return _IMG_RE.sub(repl, md)


def build_html(md_path: Path, charts_dir: Path, title: str) -> str:
    md = md_path.read_text(encoding="utf-8")
    md = _inline_images(md, charts_dir)
    md_b64 = base64.b64encode(md.encode("utf-8")).decode("ascii")
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
<div id="content"></div>
<script>
const b64 = "{md_b64}";
const md = new TextDecoder("utf-8").decode(Uint8Array.from(atob(b64), c => c.charCodeAt(0)));
marked.setOptions({{ gfm: true, breaks: false }});
document.getElementById("content").innerHTML = marked.parse(md);
</script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("charts_dir")
    ap.add_argument("out")
    ap.add_argument("--title", default="TiDB Cloud 巡检报告")
    args = ap.parse_args(argv)
    html = build_html(Path(args.report), Path(args.charts_dir), args.title)
    Path(args.out).write_text(html, encoding="utf-8")
    sys.stderr.write(f"wrote {args.out} ({len(html):,} bytes)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
