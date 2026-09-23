"""The served documentation.

Two kinds of check. The routing ones matter because the static file handler answers every unknown
path with the dashboard's page, so a documentation route registered in the wrong place would
return a web application while appearing to work -- and a test asserting only on a status code
would pass.

The rest enforce the house style, which was specified rather than assumed: scientific register,
every abbreviation defined before it is used, and no jargon left to the reader to look up
elsewhere. Prose rots faster than code, and nothing else in this suite reads it.
"""
import json
import re
import urllib.error
import urllib.request

import pytest

from gr_autopilot import wiki
from gr_autopilot.telemetry.server import serve

PAGE_NAMES = [name for name, _ in wiki.PAGES]


def _source(name: str) -> str:
    return (wiki.wiki_root() / f"{name}.md").read_text(encoding="utf-8")


def _prose(text: str) -> str:
    """The page with code blocks, inline code and tables removed.

    Style rules apply to sentences a reader reads, not to identifiers. ``sps`` inside backticks is
    a field name; "sps" in a sentence is an undefined abbreviation.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("|"))
    return text


# -- the pages exist and render ----------------------------------------------

@pytest.mark.parametrize("name", PAGE_NAMES)
def test_every_declared_page_has_a_source_file(name):
    assert (wiki.wiki_root() / f"{name}.md").is_file()


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_every_page_renders_with_a_title_and_navigation(name):
    page = wiki.render_page(name)
    assert page is not None
    assert page.title and page.title != name
    assert "<nav>" in page.html
    assert f'href="/wiki/{name}"' in page.html         # its own entry is present
    assert "<h1>" in page.html


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_the_title_is_not_repeated_in_the_body(name):
    """The heading is rendered by the template, so leaving it in the body would print it twice."""
    assert wiki.render_page(name).html.count("<h1>") == 1


def test_an_unknown_page_renders_nothing_rather_than_something_wrong():
    assert wiki.render_page("no-such-page") is None


def test_internal_links_point_at_pages_that_exist():
    known = set(PAGE_NAMES)
    for name in PAGE_NAMES:
        for target in re.findall(r"\(/wiki/([a-z-]+)\)", _source(name)):
            assert target in known, f"{name}.md links to a missing page: {target}"


# -- the writing rules -------------------------------------------------------

#: Abbreviations that must be spelled out before, or at, first use. The pattern is the
#: abbreviation as it appears in running prose.
ABBREVIATIONS = {
    "BER": "bit error ratio",
    "EVM": "error vector magnitude",
    "SNR": "signal-to-noise ratio",
    "CFO": "carrier frequency offset",
    "AGC": "automatic gain control",
    "AMC": "adaptive modulation and coding",
    "FEC": "forward error correction",
    "RRC": "root-raised-cosine",
    "MCP": "Model Context Protocol",
    "GRC": "GNU Radio Companion",
    "SDR": "software-defined radio",
    "IIO": "industrial input/output",
    "ZC": "Zadoff",
}


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_no_abbreviation_is_used_in_prose_before_it_is_defined(name):
    """The brief was explicit: explain abbreviations first, as a paper would. The glossary is
    where they are defined; any other page using one in a sentence must expand it there too."""
    prose = _prose(_source(name))
    for abbr, expansion in ABBREVIATIONS.items():
        uses = [m.start() for m in re.finditer(rf"\b{abbr}\b", prose)]
        if not uses:
            continue
        first_definition = prose.lower().find(expansion.lower())
        assert first_definition != -1, (
            f"{name}.md uses {abbr!r} in prose without ever writing {expansion!r}")
        assert first_definition < uses[0], (
            f"{name}.md uses {abbr!r} before defining it as {expansion!r}")


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_pages_avoid_unexplained_shorthand(name):
    """Terms that read as insider vocabulary. Each is fine once introduced, and the glossary
    introduces them; using one cold in another page is what this catches."""
    prose = _prose(_source(name)).lower()
    for term, expansion in (("modcod", "modulation and coding"),
                            ("es/n0", "energy per symbol"),
                            ("psd", "power spectral density")):
        if term in prose:
            assert expansion.lower() in prose, (
                f"{name}.md uses {term!r} without introducing {expansion!r}")


def test_the_glossary_defines_every_abbreviation_the_tests_track():
    glossary = _source("glossary")
    for abbr, expansion in ABBREVIATIONS.items():
        assert expansion.lower() in glossary.lower(), f"glossary is missing {expansion!r}"
        assert abbr in glossary, f"glossary never shows the abbreviation {abbr!r}"


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_every_page_opens_by_saying_what_it_is_for(name):
    """A reader arriving cold should learn the purpose of the page before its details."""
    body = _source(name).split("\n", 1)[1].strip()
    first_paragraph = body.split("\n\n")[0]
    assert len(first_paragraph) > 80, f"{name}.md opens too abruptly"
    assert not first_paragraph.startswith("#"), f"{name}.md opens with a heading, not prose"


def test_the_limitations_page_is_substantive():
    """It bounds what the results mean; a thin one would be worse than none."""
    text = _source("limitations")
    assert len(text) > 2500
    assert text.count("\n## ") >= 5


# -- serving -----------------------------------------------------------------

def _get(url):
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_the_wiki_is_served_and_does_not_shadow_the_dashboard():
    httpd = serve(None, None, port=8153, background=True)
    try:
        status, body = _get("http://127.0.0.1:8153/wiki")
        assert status == 200
        assert "<nav>" in body and "gr-" in body

        for name, _title in wiki.PAGES:
            status, body = _get(f"http://127.0.0.1:8153/wiki/{name}")
            assert status == 200, name
            assert "<h1>" in body, name

        # An unknown page must be a 404, NOT the dashboard served by the catch-all.
        status, body = _get("http://127.0.0.1:8153/wiki/does-not-exist")
        assert status == 404
        assert "No such page" in body

        # ...and the dashboard is still reachable at the root.
        status, body = _get("http://127.0.0.1:8153/")
        assert status == 200 and "gr-autopilot" in body
    finally:
        httpd.shutdown()


def test_the_page_index_reports_what_exists():
    index = wiki.page_index()
    assert len(index) == len(wiki.PAGES)
    assert all(p["exists"] for p in index), [p for p in index if not p["exists"]]
    assert all(p["url"].startswith("/wiki/") for p in index)


# -- the library page IS the catalogue ---------------------------------------

def test_library_page_carries_every_exercise_in_full():
    import html as _html
    from gr_autopilot import wiki
    from gr_autopilot.tools import prompts
    page = wiki.render_page("library")
    assert page is not None
    for p in prompts.PROMPTS:
        assert p.name in page.html, p.name
        assert _html.escape(p.title) in page.html, p.title
        # the first sentence of the prompt, as the agent gets it, is on the page
        first = p.render().split("\n")[0].split(".")[0]
        assert _html.escape(first[:50]) in page.html, (p.name, first)
    assert page.html.count("On the bench:") == sum(1 for p in prompts.PROMPTS if p.notes)
    assert wiki.CATALOG_MARKER not in page.html


# -- the portal renders the wiki inside its own shell -------------------------

def test_wiki_pages_are_served_as_data_to_the_portal_and_browsers_are_sent_there():
    """Three clients, three answers from one URL: the portal asks for ?fragment=1 and gets the
    page as JSON; a browser navigating to /wiki/<name> is redirected into the portal so the
    documentation shares the dashboard's layout; anything else (curl, a client without script)
    still gets the standalone page."""
    import json as _json
    httpd = serve(None, None, port=8154, background=True)
    try:
        # the portal's request
        status, body = _get("http://127.0.0.1:8154/wiki/library?fragment=1")
        assert status == 200
        frag = _json.loads(body)
        assert frag["name"] == "library" and frag["title"] and "<h2" in frag["body"]
        assert "<h1" not in frag["body"]                       # the title is not repeated
        assert [p["name"] for p in frag["pages"]] == [n for n, _ in wiki.PAGES]
        assert frag["prev"]["name"] == "devices" and frag["next"]["name"] == "integrity"
        status, body = _get("http://127.0.0.1:8154/wiki/does-not-exist?fragment=1")
        assert status == 404 and "pages" in _json.loads(body)

        # a browser navigation
        req = urllib.request.Request("http://127.0.0.1:8154/wiki/getting-started",
                                     headers={"Accept": "text/html,application/xhtml+xml"})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=5) as r:
                status, location = r.status, r.headers["Location"]
        except urllib.error.HTTPError as e:              # urllib reports a stopped redirect this way
            status, location = e.code, e.headers["Location"]
        assert status == 302 and location == "/#/docs/getting-started"

        # everything else: the standalone page, unchanged
        status, body = _get("http://127.0.0.1:8154/wiki/getting-started")
        assert status == 200 and "<h1>" in body and "<nav>" in body
    finally:
        httpd.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None
