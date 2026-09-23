"""Rendering the documentation served at ``/wiki``.

The source is a set of Markdown files under ``docs/wiki``. They are rendered on request rather
than pre-built into the repository, so the documentation cannot drift from its source, and so
editing a page needs nothing but a text editor.

The renderer is deliberately small: a page template, a table of contents built from the headings,
and navigation generated from the page order declared below. It uses the ``markdown`` package
that is already installed system-wide, and falls back to serving the raw text inside a preformatted
block if that package is missing, so the documentation is never simply unavailable.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

#: Page order. The file name (without extension) is also the URL.
PAGES: tuple[tuple[str, str], ...] = (
    ("index", "Introduction"),
    ("glossary", "Notation and abbreviations"),
    ("getting-started", "Getting started"),
    ("architecture", "System architecture"),
    ("flowgraphs", "Signal-processing chains"),
    ("experiments", "Experiments and their records"),
    ("devices", "Radios and device health"),
    ("library", "The experiment library"),
    ("integrity", "Measurement integrity"),
    ("limitations", "Limitations"),
)

_TITLES = dict(PAGES)


def wiki_root() -> Path:
    return Path(__file__).resolve().parents[1] / "docs" / "wiki"


@dataclass
class Rendered:
    title: str
    html: str


def _nav(current: str) -> str:
    items = []
    for name, title in PAGES:
        cls = ' class="current"' if name == current else ""
        items.append(f'<li{cls}><a href="/wiki/{name}">{html.escape(title)}</a></li>')
    return "<nav><ol>" + "".join(items) + "</ol></nav>"


def _pager(current: str) -> str:
    names = [n for n, _ in PAGES]
    if current not in names:
        return ""
    i = names.index(current)
    parts = []
    if i > 0:
        parts.append(f'<a class="prev" href="/wiki/{names[i-1]}">&larr; {html.escape(_TITLES[names[i-1]])}</a>')
    if i < len(names) - 1:
        parts.append(f'<a class="next" href="/wiki/{names[i+1]}">{html.escape(_TITLES[names[i+1]])} &rarr;</a>')
    return f'<div class="pager">{"".join(parts)}</div>' if parts else ""


def _render_markdown(text: str) -> tuple[str, str]:
    """Return ``(body_html, toc_html)``."""
    try:
        import markdown
    except ImportError:                       # documentation must never be simply unavailable
        return f"<pre>{html.escape(text)}</pre>", ""
    md = markdown.Markdown(extensions=["extra", "tables", "fenced_code", "sane_lists",
                                       "admonition", "attr_list",
                                       "toc"],
                           extension_configs={"toc": {"permalink": False, "toc_depth": "2-3"}})
    body = md.convert(text)
    return body, getattr(md, "toc", "")


_TOPIC_ORDER = ("paper",)
_TOPIC_TITLES = {"paper": "The paper's experiments"}
CATALOG_MARKER = "<!-- catalog -->"


def _catalog_markdown() -> str:
    """The experiment library as Markdown, generated from the same catalogue the portal and the
    MCP prompt menu serve, so the page cannot drift from what the agent is actually offered.

    Per topic: a table of names, then every exercise in full -- its arguments, the prompt exactly
    as the agent receives it with default arguments, and the operator's bench note, which is the
    one thing here the agent is never shown."""
    from gr_autopilot.tools import prompts
    cat = prompts.catalog()
    seen = {p["topic"] for p in cat}
    topics = [t for t in _TOPIC_ORDER if t in seen] + sorted(seen - set(_TOPIC_ORDER))
    out: list[str] = []
    for topic in topics:
        entries = [p for p in cat if p["topic"] == topic]
        out.append(f"### {_TOPIC_TITLES.get(topic, topic.title())}\n")
        out.append("| Name | What it asks for |\n|---|---|")
        out.extend(f"| [`{p['name']}`](#{p['name']}) | {p['description']} |" for p in entries)
        out.append("")
        for p in entries:
            out.append(f"#### {p['title']} {{: #{p['name']} }}\n")
            out.append(f"`{p['name']}`\n")
            if p["arguments"]:
                out.append("| Argument | Meaning | Default |\n|---|---|---|")
                out.extend(f"| `{a['name']}` | {a['description']} | `{a['default'] or '-'}` |"
                           for a in p["arguments"])
                out.append("")
            out.append("The prompt, with default arguments:\n")
            out.extend(("> " + line) if line else ">" for line in p["body"].split("\n"))
            out.append("")
            if p.get("notes"):
                out.append(f"**On the bench:** {p['notes']}\n")
    return "\n".join(out)


def _expand(text: str) -> str:
    if CATALOG_MARKER not in text:
        return text
    return text.replace(CATALOG_MARKER, _catalog_markdown())


def render_fragment(name: str) -> dict | None:
    """One page as data: title, body HTML, table of contents, and where it sits in the order.

    This is what the portal renders inside its own shell, so the documentation shares the
    dashboard's header, navigation and theme instead of carrying a second layout of its own.
    """
    path = wiki_root() / f"{name}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    body, toc = _render_markdown(_expand(text))
    # The first level-one heading is the page title; it is not repeated in the body.
    match = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    title = re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else _TITLES.get(name, name)
    body = re.sub(r"<h1[^>]*>.*?</h1>", "", body, count=1, flags=re.S)
    names = [n for n, _ in PAGES]
    i = names.index(name) if name in names else -1
    prev = {"name": names[i - 1], "title": _TITLES[names[i - 1]]} if i > 0 else None
    nxt = ({"name": names[i + 1], "title": _TITLES[names[i + 1]]}
           if 0 <= i < len(names) - 1 else None)
    return {"name": name, "title": title, "body": body, "toc": toc,
            "pages": page_index(), "prev": prev, "next": nxt}


def render_page(name: str) -> Rendered | None:
    """The standalone rendering, for clients that are not the portal (curl, tests, no script)."""
    frag = render_fragment(name)
    if frag is None:
        return None
    page = _TEMPLATE.format(
        title=html.escape(frag["title"]), nav=_nav(name),
        toc=f'<aside class="toc"><h2>On this page</h2>{frag["toc"]}</aside>' if frag["toc"] else "",
        body=frag["body"], pager=_pager(name), css=_CSS)
    return Rendered(title=frag["title"], html=page)


def page_index() -> list[dict]:
    root = wiki_root()
    return [{"name": n, "title": t, "url": f"/wiki/{n}", "exists": (root / f"{n}.md").is_file()}
            for n, t in PAGES]


_CSS = """
:root { color-scheme: dark; --bg:#0b0f15; --fg:#dbe4ee; --muted:#8798a8; --line:#1e2a38;
        --card:#111823; --accent:#38bdf8; --code:#0d141d; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { display:grid; grid-template-columns: 250px minmax(0,1fr) 220px; gap:34px; max-width:1400px; margin:0 auto; padding:28px 24px 80px; }
header.top { border-bottom:1px solid var(--line); padding:14px 24px; display:flex; gap:18px; align-items:center; }
header.top .brand { font-family:ui-monospace,Menlo,Consolas,monospace; font-weight:600; font-size:17px; }
header.top .brand span { color:var(--accent); }
header.top a { color:var(--muted); text-decoration:none; font-size:13.5px; }
header.top a:hover { color:var(--fg); }
nav ol { list-style:none; margin:0; padding:0; position:sticky; top:24px; counter-reset:s; }
nav li { counter-increment:s; margin:0 0 2px; }
nav li a { display:block; padding:6px 10px; border-radius:7px; color:var(--muted); text-decoration:none; font-size:14px; }
nav li a::before { content:counter(s) ". "; color:#4a5b6d; }
nav li a:hover { background:var(--card); color:var(--fg); }
nav li.current a { background:var(--card); color:var(--fg); box-shadow:inset 2px 0 0 var(--accent); }
main { min-width:0; }
main h1 { font-size:30px; line-height:1.25; margin:0 0 8px; letter-spacing:-.3px; }
main h2 { font-size:21px; margin:34px 0 10px; padding-top:10px; border-top:1px solid var(--line); letter-spacing:-.2px; }
main h3 { font-size:16.5px; margin:22px 0 6px; }
main p, main li { color:#c8d4e0; }
main a { color:var(--accent); }
code { background:var(--code); padding:1px 5px; border-radius:4px; font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13.5px; }
pre { background:var(--code); border:1px solid var(--line); border-radius:10px; padding:13px 15px; overflow-x:auto; }
pre code { background:none; padding:0; font-size:13px; line-height:1.55; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14.5px; display:block; overflow-x:auto; }
th, td { border:1px solid var(--line); padding:7px 11px; text-align:left; vertical-align:top; }
th { background:var(--card); font-weight:600; }
blockquote { margin:16px 0; padding:10px 16px; border-left:3px solid var(--accent); background:var(--card); border-radius:0 8px 8px 0; }
blockquote p { margin:6px 0; }
.admonition { margin:16px 0; padding:12px 16px; border-radius:9px; background:var(--card); border:1px solid var(--line); }
.admonition-title { font-weight:600; margin:0 0 6px; font-size:14px; text-transform:uppercase; letter-spacing:.6px; color:var(--accent); }
.admonition.warning { border-color:#f5b13d55; } .admonition.warning .admonition-title { color:#f5b13d; }
aside.toc { font-size:13px; }
aside.toc h2 { font-size:11px; text-transform:uppercase; letter-spacing:1.2px; color:var(--muted); border:0; margin:2px 0 8px; padding:0; }
aside.toc ul { list-style:none; margin:0; padding:0; position:sticky; top:24px; }
aside.toc li { margin:3px 0; }
aside.toc a { color:var(--muted); text-decoration:none; }
aside.toc a:hover { color:var(--fg); }
aside.toc ul ul { padding-left:11px; }
.pager { display:flex; justify-content:space-between; gap:16px; margin-top:44px; padding-top:18px; border-top:1px solid var(--line); font-size:14px; }
.pager a { color:var(--accent); text-decoration:none; }
dt { font-weight:600; margin-top:12px; }
dd { margin:2px 0 0 0; color:#c8d4e0; }
@media (max-width:1080px){ .wrap{ grid-template-columns:1fr; } aside.toc{ display:none; } nav ol{ position:static; } }
"""

_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — gr-autopilot</title>
<style>{css}</style>
</head><body>
<header class="top">
  <span class="brand">gr-<span>autopilot</span></span>
  <a href="/wiki/index">Documentation</a>
  <a href="/">Dashboard</a>
  <a href="/experiments">Experiments</a>
  <a href="/prompts">Experiment library</a>
</header>
<div class="wrap">
{nav}
<main><h1>{title}</h1>
{body}
{pager}
</main>
{toc}
</div>
</body></html>
"""
