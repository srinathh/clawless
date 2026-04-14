"""Wiki endpoint — serves markdown files from ~/workspace/wiki as HTML."""

from __future__ import annotations

from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

_MD = markdown.Markdown(extensions=["fenced_code", "tables", "toc"])

_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ max-width: 860px; margin: 2rem auto; padding: 0 1rem;
          font-family: system-ui, sans-serif; line-height: 1.6; color: #222; }}
  h1, h2, h3 {{ line-height: 1.2; }}
  a {{ color: #0969da; }}
  pre {{ background: #f6f8fa; padding: 1rem; overflow-x: auto; border-radius: 6px; }}
  code {{ background: #f6f8fa; padding: .2em .4em; border-radius: 4px; font-size: 90%; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #d0d7de; padding: .4rem .6rem; }}
  th {{ background: #f6f8fa; }}
  .breadcrumb {{ font-size: .9em; margin-bottom: 1.5rem; color: #57606a; }}
  .breadcrumb a {{ color: inherit; }}
  ul.index {{ list-style: none; padding: 0; }}
  ul.index li {{ padding: .25rem 0; border-bottom: 1px solid #f0f0f0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _render(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(_HTML_TEMPLATE.format(title=title, body=body_html))


def make_wiki_router(workspace: Path) -> APIRouter:
    """Return an APIRouter that serves the wiki from <workspace>/wiki."""
    router = APIRouter(prefix="/wiki")
    wiki_root = workspace / "wiki"

    def _wiki_dir() -> Path:
        if not wiki_root.is_dir():
            raise HTTPException(status_code=404, detail="Wiki directory not found")
        return wiki_root

    @router.get("", response_class=HTMLResponse)
    async def wiki_index(request: Request) -> HTMLResponse:
        root = _wiki_dir()
        pages = sorted(root.rglob("*.md"))
        if not pages:
            return _render("Wiki", "<h1>Wiki</h1><p>No pages yet.</p>")

        items = []
        for p in pages:
            rel = p.relative_to(root)
            href = f"/wiki/{rel.with_suffix('')}"
            items.append(f'<li><a href="{href}">{rel}</a></li>')

        body = "<h1>Wiki</h1>\n<ul class='index'>\n" + "\n".join(items) + "\n</ul>"
        return _render("Wiki", body)

    @router.get("/{page_path:path}", response_class=HTMLResponse)
    async def wiki_page(page_path: str, request: Request) -> HTMLResponse:
        root = _wiki_dir()
        # Accept with or without .md extension
        candidates = [
            root / page_path,
            root / (page_path + ".md"),
        ]
        md_file: Path | None = next((p for p in candidates if p.is_file() and p.suffix == ".md"), None)
        if md_file is None:
            raise HTTPException(status_code=404, detail=f"Wiki page '{page_path}' not found")

        # Prevent path traversal outside wiki root
        try:
            md_file.resolve().relative_to(wiki_root.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        source = md_file.read_text(encoding="utf-8")
        _MD.reset()
        content_html = _MD.convert(source)

        title = md_file.stem.replace("-", " ").replace("_", " ").title()
        rel = md_file.relative_to(root)
        breadcrumb = (
            f'<p class="breadcrumb"><a href="/wiki">Wiki</a> / {rel}</p>'
        )
        return _render(title, breadcrumb + content_html)

    return router
