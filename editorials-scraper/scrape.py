"""
Editorials aggregator — E2: per-source scrapers.

Fetches the freshest editorial + opinion pieces from 4 Indian English dailies
and prints structured Article records to stdout (for the GitHub Actions log).
Markdown emission lands at E3; for E2 the file still gets the E1 stub.

Source set (4 papers × 2 streams):
  - The Hindu              — editorial (RSS) + lead (RSS)
  - Deccan Chronicle       — DC Edit (RSS-filtered) + columnist (RSS-filtered)
  - Deccan Herald          — editorial.rss (full body) + opinion.rss (filtered)
  - The New Indian Express — listing-scrape (no RSS) for both editorial + column

Production constraints:
  - Datacenter IP (GH Actions on Azure). Sites that WAF-rejected at
    workstation may behave differently here. Every per-source function is
    wrapped — if one publisher 403s, the run logs the failure and continues.
  - stdlib-only HTTP via urllib.request — no `requests` dependency.
  - Per-source extractors lifted from the workstation prototype after the
    user signed off on quality on 2026-05-15.
"""

from __future__ import annotations

import gzip
import html as ihtml
import json
import re
import sys
import traceback
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen


# ── Config ──────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)
DEFAULT_TIMEOUT = 25

# Per-stream item budget — captures any back-dated entries plus same-day
# publication. Editorials are typically 1/day per source; opinion sections
# carry multiple columnists, so 3 catches the freshest few.
N_EDITORIAL = 2
N_OPINION = 3

OUTPUT_FILE = Path(__file__).resolve().parent.parent / "editorials.md"


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class Article:
    source: str           # "The Hindu", "Deccan Chronicle", ...
    type: str             # "editorial" or "opinion"
    title: str
    url: str
    published_at: str     # RFC 822 (RSS pubDate) or ISO 8601 — whichever the source gave us
    paragraphs: list[str] = field(default_factory=list)

    @property
    def body_chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)


# ── HTTP ────────────────────────────────────────────────────────────────────


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Browser-headers HTTP GET. Raises HTTPError / URLError on failure —
    let the per-source wrapper catch and fail soft."""
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            # Skip brotli — Python stdlib doesn't decode it.
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        data = gzip.decompress(data)
    elif enc == "deflate":
        data = zlib.decompress(data)
    return data


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", errors="ignore")


# ── HTML cleanup (shared) ───────────────────────────────────────────────────

_NOISE_TAGS = ("script", "style", "noscript", "iframe", "ins", "form")
_JS_HINTS = (
    "googletag", "userIdentify", "addEventListener", "taboola",
    "function(", "function (", '$("', "$('", "window.__",
    "isDeviceEnabled", "isNonSubcribedUser", "googletagcmd",
    "document.getElementById", "document.write",
)
_CHROME_WORDS = (
    "Facebook", "Twitter", "WhatsApp", "Telegram", "LinkedIn",
    "Reddit", "Pinterest", "Copy link", "Read Comments", "READ LATER",
    "SEE ALL", "Related Topics", "Sign in", "Sign up", "Subscribe",
)
_META_PREFIXES = ("Published -", "Published-", "Updated -", "Updated-", "Posted -")


def is_chrome_paragraph(p: str) -> bool:
    if any(p.startswith(prefix) for prefix in _META_PREFIXES):
        return True
    if sum(1 for w in _CHROME_WORDS if w in p) >= 2:
        return True
    if p.count(" / ") >= 4:
        return True
    return False


def html_to_paragraphs(s: str) -> list[str]:
    """Strip noise blocks → decode entities → split on </p> | <br> | blank
    line | double-space → filter chrome + minified-JS survivors.

    The strip-noise-blocks-WHOLE step is load-bearing: a naive `<[^>]+>`
    tag-removal regex strips only the tag pairs, leaving inline JS text as
    fake paragraphs (Hindu and TOI both inline googletag / taboola / lightbox
    handlers between every real paragraph). Cleanup pass validated by the
    user on 2026-05-15.
    """
    if "<" in s or "&lt;" in s:
        s = ihtml.unescape(s)
        for tag in _NOISE_TAGS:
            s = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", s, flags=re.DOTALL | re.I)
            s = re.sub(rf"<{tag}\b[^>]*/>", "", s, flags=re.I)
        s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
        parts = re.split(r"</p>|</?br\s*/?>|\n\n+", s)
    else:
        parts = re.split(r"\n\n+|  +", s)
    out: list[str] = []
    for p in parts:
        clean = re.sub(r"<[^>]+>", "", p)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) <= 20:
            continue
        if any(hint in clean for hint in _JS_HINTS):
            continue
        if clean.count("}") > 3 and clean.count("{") > 2:
            continue
        if is_chrome_paragraph(clean):
            continue
        out.append(clean)
    return out


def find_jsonld_articleBody(html_text: str) -> tuple[Optional[str], str]:
    """Returns (articleBody, datePublished) from any JSON-LD blob on the page.
    Both empty if no usable NewsArticle / Article schema is present."""
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.DOTALL,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f\x7f]", "", raw))
            except Exception:
                continue
        objs = data if isinstance(data, list) else [data]
        flat: list[dict] = []
        for o in objs:
            if isinstance(o, dict):
                flat.append(o)
                if isinstance(o.get("@graph"), list):
                    flat.extend([g for g in o["@graph"] if isinstance(g, dict)])
        for obj in flat:
            ab = obj.get("articleBody")
            if isinstance(ab, str) and len(ab) > 200:
                return ab, obj.get("datePublished", "") or ""
    return None, ""


# ── The Hindu ───────────────────────────────────────────────────────────────


def _scrape_hindu_article(url: str) -> list[str]:
    html_text = fetch_text(url)
    m = re.search(
        r'<div\s+id="content-body-[^"]+"[^>]*>(.*?)<div\s+class="end-article',
        html_text, re.DOTALL,
    )
    if not m:
        m = re.search(
            r'<div\s+id="content-body-[^"]+"[^>]*>(.*?)(?=<aside|<footer)',
            html_text, re.DOTALL,
        )
    return html_to_paragraphs(m.group(1) if m else "")


def fetch_hindu_editorial(n: int = N_EDITORIAL) -> list[Article]:
    raw = fetch("https://www.thehindu.com/opinion/editorial/feeder/default.rss")
    root = ET.fromstring(raw)
    results: list[Article] = []
    for item in root.findall(".//item")[:n]:
        url = item.findtext("link", "") or ""
        title = (item.findtext("title", "") or "").strip()
        pub = (item.findtext("pubDate", "") or "").strip()
        results.append(Article(
            source="The Hindu", type="editorial",
            title=title, url=url, published_at=pub,
            paragraphs=_scrape_hindu_article(url),
        ))
    return results


def fetch_hindu_opinion(n: int = N_OPINION) -> list[Article]:
    """Hindu's `lead` feed is the heavyweight opinion essay surface."""
    raw = fetch("https://www.thehindu.com/opinion/lead/feeder/default.rss")
    root = ET.fromstring(raw)
    results: list[Article] = []
    for item in root.findall(".//item")[:n]:
        url = item.findtext("link", "") or ""
        title = (item.findtext("title", "") or "").strip()
        pub = (item.findtext("pubDate", "") or "").strip()
        results.append(Article(
            source="The Hindu", type="opinion",
            title=title, url=url, published_at=pub,
            paragraphs=_scrape_hindu_article(url),
        ))
    return results


# ── Deccan Chronicle ────────────────────────────────────────────────────────


def _dc_extract(item, type_label: str) -> Article:
    url = item.findtext("link", "") or ""
    title = (item.findtext("title", "") or "").strip()
    pub = (item.findtext("pubDate", "") or "").strip()
    body, dp = find_jsonld_articleBody(fetch_text(url))
    paragraphs = html_to_paragraphs(body) if body else []
    return Article(
        source="Deccan Chronicle", type=type_label,
        title=title, url=url, published_at=pub or dp,
        paragraphs=paragraphs,
    )


def fetch_dc_editorial(n: int = N_EDITORIAL) -> list[Article]:
    raw = fetch("https://www.deccanchronicle.com/rss_feed/opinion-69.xml")
    root = ET.fromstring(raw)
    matched = []
    for it in root.findall(".//item"):
        if "/dc-comment/dc-edit" in (it.findtext("link", "") or ""):
            matched.append(it)
        if len(matched) >= n:
            break
    return [_dc_extract(it, "editorial") for it in matched]


def fetch_dc_opinion(n: int = N_OPINION) -> list[Article]:
    raw = fetch("https://www.deccanchronicle.com/rss_feed/opinion-69.xml")
    root = ET.fromstring(raw)
    matched = []
    for it in root.findall(".//item"):
        link = it.findtext("link", "") or ""
        if "/opinion/columnists/" in link or "/opinion/op-ed/" in link:
            matched.append(it)
        if len(matched) >= n:
            break
    return [_dc_extract(it, "opinion") for it in matched]


# ── Deccan Herald ───────────────────────────────────────────────────────────

_DH_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}


def _dh_extract(item, type_label: str) -> Article:
    url = item.findtext("link", "") or ""
    title = (item.findtext("title", "") or "").strip()
    pub = (item.findtext("pubDate", "") or "").strip()
    # DH ships full body in <content:encoded> for most items.
    body = item.findtext("content:encoded", "", _DH_NS) or ""
    paragraphs = html_to_paragraphs(body)
    if not paragraphs:
        # Fall back to article-scrape if the feed item somehow lacks a body.
        body, _ = find_jsonld_articleBody(fetch_text(url))
        paragraphs = html_to_paragraphs(body) if body else []
    return Article(
        source="Deccan Herald", type=type_label,
        title=title, url=url, published_at=pub,
        paragraphs=paragraphs,
    )


_DH_EDITORIAL_URL_HINTS = ("/opinion/editorial", "/dh-edit", "/opinion/dh-views")


def fetch_dh_editorial(n: int = N_EDITORIAL) -> list[Article]:
    """Prefer the editorial-only feed; fall back to opinion.rss with URL
    filter if editorial.rss is missing or empty."""
    try:
        raw = fetch("https://www.deccanherald.com/api/v1/collections/editorial.rss")
        root = ET.fromstring(raw)
        items = root.findall(".//item")[:n]
    except Exception:
        items = []
    if not items:
        raw = fetch("https://www.deccanherald.com/api/v1/collections/opinion.rss")
        root = ET.fromstring(raw)
        items = []
        for it in root.findall(".//item"):
            if any(hint in (it.findtext("link", "") or "") for hint in _DH_EDITORIAL_URL_HINTS):
                items.append(it)
            if len(items) >= n:
                break
    return [_dh_extract(it, "editorial") for it in items]


def fetch_dh_opinion(n: int = N_OPINION) -> list[Article]:
    raw = fetch("https://www.deccanherald.com/api/v1/collections/opinion.rss")
    root = ET.fromstring(raw)
    matched = []
    for it in root.findall(".//item"):
        link = it.findtext("link", "") or ""
        if any(hint in link for hint in _DH_EDITORIAL_URL_HINTS):
            continue  # editorial — skip
        matched.append(it)
        if len(matched) >= n:
            break
    return [_dh_extract(it, "opinion") for it in matched]


# ── The New Indian Express ──────────────────────────────────────────────────

_NIE_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _nie_sort_key(url: str) -> tuple[int, int, int]:
    m = re.search(r"/(\d{4})/([A-Z][a-z]{2})/(\d{1,2})/", url)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), _NIE_MONTHS.get(m.group(2), 0), int(m.group(3)))


_NIE_CHROME_STARTS = ("copied", "follow the new indian express", "click here", "also read")


def _nie_extract(url: str, type_label: str) -> Article:
    html_text = fetch_text(url)
    # Title + date from the NewsArticle JSON-LD blob (more reliable than the
    # SPA-rendered <h1> NIE wraps).
    title, dp = "", ""
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.DOTALL,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(data, dict) and data.get("@type") == "NewsArticle":
            title = (data.get("headline") or "").strip()
            dp = (data.get("datePublished") or "").strip()
            break
    # NIE's JSON-LD articleBody is one continuous string with no paragraph
    # delimiters — useless for rendering. Parse the page's <p> tags instead.
    p_blocks = re.findall(r"<p[^>]*>(.*?)</p>", html_text, re.DOTALL)
    paragraphs: list[str] = []
    for raw in p_blocks:
        clean = re.sub(r"<[^>]+>", "", raw)
        clean = ihtml.unescape(clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) < 80:
            continue
        if any(clean.lower().startswith(s) for s in _NIE_CHROME_STARTS):
            continue
        if is_chrome_paragraph(clean):
            continue
        paragraphs.append(clean)
    return Article(
        source="The New Indian Express", type=type_label,
        title=title, url=url, published_at=dp,
        paragraphs=paragraphs,
    )


def fetch_nie_editorial(n: int = N_EDITORIAL) -> list[Article]:
    listing = fetch_text("https://www.newindianexpress.com/opinion/editorials")
    hrefs = re.findall(
        r'href=["\']?(https?://www\.newindianexpress\.com/opinion/editorials/\d{4}/[^"\'\s]+)',
        listing,
    )
    seen, ordered = set(), []
    for u in hrefs:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    ordered.sort(key=_nie_sort_key, reverse=True)
    return [_nie_extract(u, "editorial") for u in ordered[:n]]


def fetch_nie_opinion(n: int = N_OPINION) -> list[Article]:
    listing = fetch_text("https://www.newindianexpress.com/opinion/columns")
    # NIE column URLs sit under /opinion/columns/<columnist-slug>/YYYY/Mon/DD/slug
    # (e.g. /opinion/columns/pc/2026/...) or directly at /opinion/columns/YYYY/...
    hrefs = re.findall(
        r'href=["\']?(https?://www\.newindianexpress\.com/opinion/columns/[^"\'\s]+/\d{4}/[A-Z][a-z]{2}/\d{1,2}/[^"\'\s]+)',
        listing,
    )
    if not hrefs:
        hrefs = re.findall(
            r'href=["\']?(https?://www\.newindianexpress\.com/opinion/columns/\d{4}/[A-Z][a-z]{2}/\d{1,2}/[^"\'\s]+)',
            listing,
        )
    seen, ordered = set(), []
    for u in hrefs:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    ordered.sort(key=_nie_sort_key, reverse=True)
    return [_nie_extract(u, "opinion") for u in ordered[:n]]


# ── Orchestrator ────────────────────────────────────────────────────────────

SCRAPERS: list[tuple[str, Callable[[], list[Article]]]] = [
    ("Hindu editorial", fetch_hindu_editorial),
    ("Hindu opinion (lead)", fetch_hindu_opinion),
    ("DC editorial", fetch_dc_editorial),
    ("DC opinion (columnist)", fetch_dc_opinion),
    ("DH editorial", fetch_dh_editorial),
    ("DH opinion", fetch_dh_opinion),
    ("NIE editorial", fetch_nie_editorial),
    ("NIE opinion (column)", fetch_nie_opinion),
]


def write_stub() -> None:
    """E1-compatible stub. E3 replaces this with the real markdown writer
    that consumes the Article records produced above."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = (
        "# Editorials\n\n"
        f"_Last updated: {now}_\n\n"
        "This file is the aggregated daily editorial / opinion feed for "
        "the Joey app's `joey.editorials` plugin. It is regenerated daily "
        "by `.github/workflows/fetch-editorials.yml`.\n\n"
        "**Scaffold only (Layer E2 — scrapers verified; markdown writer "
        "lands at E3).**\n"
    )
    OUTPUT_FILE.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT_FILE} ({len(content)} bytes)")


def main() -> int:
    print("=" * 60)
    print(f"Editorials aggregator — E2 scraper run @ {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    all_articles: list[Article] = []
    failures: list[str] = []
    for name, fn in SCRAPERS:
        try:
            items = fn()
            if not items:
                print(f"  ⚠ [{name}] returned 0 items")
                failures.append(f"{name}: 0 items")
                continue
            for a in items:
                print(f"  ✓ [{name}] {a.title[:64]} — {a.body_chars} chars / {len(a.paragraphs)} ¶")
            all_articles.extend(items)
        except Exception as e:
            print(f"  ✗ [{name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            failures.append(f"{name}: {type(e).__name__}")

    print()
    print(f"=== Total: {len(all_articles)} articles "
          f"across {len(SCRAPERS) - len(failures)}/{len(SCRAPERS)} streams")
    if failures:
        print(f"=== Failures: {', '.join(failures)}")

    # E1 verification path — still write the stub. E3 replaces this with
    # the real markdown writer using `all_articles`.
    write_stub()

    # Exit non-zero only if every stream failed — partial success is OK.
    if failures and len(failures) == len(SCRAPERS):
        print("ERROR: all streams failed — bailing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
