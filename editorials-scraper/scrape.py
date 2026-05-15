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
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
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

# Drop articles whose extracted body is shorter than this. Caught at E2:
# DH's "Speak Out" letters-compilation is 45 chars; a real editorial is
# always > 1500 chars. 500 catches stub content without false negatives.
MIN_BODY_CHARS = 500

# Source order for stable per-date sorting (matches the 4-paper v1 set).
_SOURCE_RANK = {
    "The Hindu": 0,
    "Deccan Chronicle": 1,
    "Deccan Herald": 2,
    "The New Indian Express": 3,
}
_TYPE_RANK = {"editorial": 0, "opinion": 1}

# Indian Standard Time — display dates in IST since the audience is Indian.
IST = timezone(timedelta(hours=5, minutes=30))

# Rolling window for the live editorials.md. Items older than this move to
# the monthly archive in archive/editorials-YYYY-MM.md.
WINDOW_DAYS = 90

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "editorials.md"
ARCHIVE_DIR = REPO_ROOT / "archive"


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


# ── Date helpers ────────────────────────────────────────────────────────────


def parse_published_at(s: str) -> Optional[datetime]:
    """Parse RFC 822 (RSS pubDate) → ISO 8601 (JSON-LD datePublished) →
    '15 May 2026' (rendered date format from prior runs of this script).
    Returns timezone-aware datetime in source TZ; None if unparseable.

    Single source of truth used by `_sort_key`, `_ist_date_key`, and the
    archive cutoff math — keep them all going through this one parser so
    they can't drift apart.
    """
    s = (s or "").strip()
    if not s:
        return None
    # RFC 822: "Thu, 15 May 2026 14:29:56 +0530"
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (TypeError, ValueError):
        pass
    # ISO 8601: "2026-05-15T20:31:13Z" or "2026-05-15T20:31:13+05:30"
    try:
        normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Rendered-date format produced by render_markdown: "15 May 2026" or "5 May 2026".
    try:
        return datetime.strptime(s, "%d %B %Y").replace(tzinfo=IST)
    except ValueError:
        return None


# ── Markdown roundtrip ──────────────────────────────────────────────────────

_HEADER_TMPL = (
    "# Editorials\n\n"
    "_Last updated: {now}_\n\n"
    "Aggregated daily editorial / opinion feed for the Joey app's "
    "`joey.editorials` plugin. Regenerated daily at 7 AM IST by "
    "`.github/workflows/fetch-editorials.yml`.\n\n"
)

# Recognise YYYY-MM-DD or "15 May 2026" date headers. Strict match for ## DATE
# (with at least one space after ##) so we don't mistake article titles for
# date headers.
_DATE_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def parse_existing_md(text: str) -> list["Article"]:
    """Parse editorials.md back into Article records for merge-and-rewrite.

    Tolerant by design — items whose source/type/url can't be parsed are
    skipped silently (with a log line) rather than crashing the run. Matches
    the "self-heal at read time, don't crash" principle from the Joey
    architecture docs.
    """
    articles: list[Article] = []
    if not text.strip():
        return articles

    # Split into date sections. Each section is everything between one
    # ##-date header and the next.
    matches = list(_DATE_HEADER_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        date_str = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((date_str, text[body_start:body_end]))

    for date_str, body in sections:
        # Each item ends at "\n---\n" or EOF.
        item_blocks = re.split(r"\n---+\s*\n", body)
        for raw in item_blocks:
            block = raw.strip()
            if not block:
                continue
            # Title: ### title (first line)
            title_match = re.match(r"^###\s+(.+?)\s*$", block, re.MULTILINE)
            if not title_match:
                continue
            title = title_match.group(1).strip()
            after_title = block[title_match.end():].lstrip()
            # Source line: *Source · Type*  (first line after title)
            source_match = re.match(r"\*([^*\n]+)\*\s*$", after_title, re.MULTILINE)
            if not source_match:
                # No source line — skip this block (corrupted entry)
                print(f"  ⚠ skipping un-parseable item '{title[:50]}' (no source line)", file=sys.stderr)
                continue
            source_line = source_match.group(1).strip()
            # source_line is "Source · Type" — split on " · "
            parts = [p.strip() for p in source_line.split("·")]
            if len(parts) < 2:
                print(f"  ⚠ skipping '{title[:50]}' (malformed source line: {source_line!r})", file=sys.stderr)
                continue
            source = parts[0]
            article_type = parts[1].lower()
            after_source = after_title[source_match.end():].strip()
            # URL: [Read full article](URL)
            url_match = re.search(r"\[Read full article\]\((https?://[^\)]+)\)", after_source)
            url = url_match.group(1).strip() if url_match else ""
            # Paragraphs: everything between source line and URL, split on blank lines.
            content_region = after_source[:url_match.start()] if url_match else after_source
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content_region.strip()) if p.strip()]
            articles.append(Article(
                source=source,
                type=article_type,
                title=title,
                url=url,
                published_at=date_str,  # we only stored the rendered date — good enough for dedup + sort
                paragraphs=paragraphs,
            ))
    return articles


def _sort_key(a: "Article") -> tuple:
    """Order: newest date first, then by source rank, then by type rank,
    then by URL for deterministic ties.

    Date is truncated to day-only in IST so intra-day publication times don't
    reorder items within the same display group — opinion would jump above
    editorial if the opinion piece happens to publish later in the morning.
    """
    dt = parse_published_at(a.published_at) or datetime(1970, 1, 1, tzinfo=IST)
    date_only = dt.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        -date_only.timestamp(),
        _SOURCE_RANK.get(a.source, 99),
        _TYPE_RANK.get(a.type.lower(), 99),
        a.url or a.title,
    )


def _ist_date_key(a: "Article") -> str:
    """The date label under which this article gets grouped in the rendered md."""
    dt = parse_published_at(a.published_at)
    if dt is not None:
        return dt.astimezone(IST).strftime("%-d %B %Y")
    return a.published_at.strip()  # last-resort: pass through whatever we had


def render_markdown(articles: list["Article"]) -> str:
    """Render the merged Article list into the canonical editorials.md format.

    Schema mirrors `current-affairs.md` (consumed by the existing Daily News
    parser at `apps/joey/src/plugins/daily-news/currentAffairs.ts`) so the
    Editorials plugin can fork-and-tweak that parser at E5 instead of writing
    a fresh one. Only difference: source line is `*Source · Type*` with Type
    being "Editorial" or "Opinion" (Title-case).
    """
    sorted_articles = sorted(articles, key=_sort_key)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [_HEADER_TMPL.format(now=now)]

    # Group by date (preserving sort order).
    current_date = None
    for a in sorted_articles:
        date_label = _ist_date_key(a)
        if date_label != current_date:
            out.append(f"## {date_label}\n\n")
            current_date = date_label

        type_titlecase = a.type.title()  # "editorial" -> "Editorial"
        out.append(f"### {a.title}\n")
        out.append(f"*{a.source} · {type_titlecase}*\n\n")
        for p in a.paragraphs:
            out.append(f"{p}\n\n")
        if a.url:
            out.append(f"[Read full article]({a.url})\n\n")
        out.append("---\n\n")

    return "".join(out)


_TIMESTAMP_LINE_RE = re.compile(r"^_Last updated:.*$", re.MULTILINE)


def _body_signature(text: str) -> str:
    """Compare-only view of editorials.md content — strips the timestamp
    line so we don't trigger spurious daily commits when only the
    'Last updated' line differs."""
    return _TIMESTAMP_LINE_RE.sub("", text).strip()


# ── Rolling window + archive ────────────────────────────────────────────────


def _archive_path(month_key: str) -> Path:
    return ARCHIVE_DIR / f"editorials-{month_key}.md"


def render_archive_markdown(articles: list["Article"], month_key: str) -> str:
    """Same per-item format as the live file, but with an archive-specific
    H1 and NO `_Last updated:` line (archives are immutable in spirit —
    once a month rolls off the live file it stops being mutated except for
    occasional late-arriving items)."""
    sorted_articles = sorted(articles, key=_sort_key)
    out = [f"# Editorials archive — {month_key}\n\n"]
    current_date = None
    for a in sorted_articles:
        date_label = _ist_date_key(a)
        if date_label != current_date:
            out.append(f"## {date_label}\n\n")
            current_date = date_label
        type_titlecase = a.type.title()
        out.append(f"### {a.title}\n")
        out.append(f"*{a.source} · {type_titlecase}*\n\n")
        for p in a.paragraphs:
            out.append(f"{p}\n\n")
        if a.url:
            out.append(f"[Read full article]({a.url})\n\n")
        out.append("---\n\n")
    return "".join(out)


def split_window(merged: list["Article"]) -> tuple[list["Article"], dict[str, list["Article"]]]:
    """Split into (in_window, archive_buckets_by_YYYY-MM).

    Date math gotcha per the plan: use the article's published_at, NOT today's
    date. Otherwise late-arriving items (e.g. a column scraped a few days
    after publication) would be measured from the wrong start.

    Items with un-parseable published_at are KEPT in the live file rather
    than silently orphaned — they'd otherwise be invisible.
    """
    cutoff_ist = (
        datetime.now(IST)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=WINDOW_DAYS)
    )
    in_window: list[Article] = []
    archive_buckets: dict[str, list[Article]] = {}
    for a in merged:
        dt = parse_published_at(a.published_at)
        if dt is None:
            in_window.append(a)
            continue
        dt_ist = dt.astimezone(IST)
        if dt_ist >= cutoff_ist:
            in_window.append(a)
        else:
            month_key = dt_ist.strftime("%Y-%m")
            archive_buckets.setdefault(month_key, []).append(a)
    return in_window, archive_buckets


def update_archives(archive_buckets: dict[str, list["Article"]]) -> int:
    """For each month bucket, merge with existing archive file (URL-dedup),
    write back if content changed. Returns count of archive files modified."""
    if not archive_buckets:
        return 0
    ARCHIVE_DIR.mkdir(exist_ok=True)
    modified = 0
    for month_key, items_for_month in sorted(archive_buckets.items()):
        path = _archive_path(month_key)
        existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        existing = parse_existing_md(existing_text)
        seen_urls = {a.url for a in existing if a.url}
        new_in_month = [a for a in items_for_month if a.url and a.url not in seen_urls]
        if not new_in_month:
            continue
        merged_for_month = existing + new_in_month
        rendered = render_archive_markdown(merged_for_month, month_key)
        if rendered.strip() == existing_text.strip():
            continue
        path.write_text(rendered, encoding="utf-8")
        print(
            f"  archived {len(new_in_month)} item(s) to {path.relative_to(REPO_ROOT)} "
            f"({len(merged_for_month)} items in month)"
        )
        modified += 1
    return modified


def write_markdown(scraped: list["Article"]) -> None:
    """Read existing editorials.md → dedup new items → merge with existing →
    split at the 90-day window → write archive files for out-of-window items
    → write the live file with only in-window items. Skip the live-file
    write if its body is unchanged so quiet days don't produce churn commits.
    """
    existing_text = OUTPUT_FILE.read_text(encoding="utf-8") if OUTPUT_FILE.exists() else ""
    existing = parse_existing_md(existing_text)
    if existing:
        print(f"  read {len(existing)} existing items from {OUTPUT_FILE.name}")

    seen_urls = {a.url for a in existing if a.url}
    new_items = [a for a in scraped if a.url and a.url not in seen_urls]
    skipped_dup = len(scraped) - len(new_items)
    if skipped_dup:
        print(f"  deduped: {skipped_dup} new-scrape items already in file")

    merged = existing + new_items

    # Roll the window — archive files first so out-of-window items are
    # durable on disk before we remove them from the live file.
    in_window, archive_buckets = split_window(merged)
    n_to_archive = sum(len(v) for v in archive_buckets.values())
    if n_to_archive:
        print(f"  rolling window: {n_to_archive} item(s) older than {WINDOW_DAYS} days")
    update_archives(archive_buckets)

    rendered = render_markdown(in_window)
    if _body_signature(rendered) == _body_signature(existing_text):
        print(f"  no new content — skipping live-file write ({len(in_window)} items unchanged)")
        return

    OUTPUT_FILE.write_text(rendered, encoding="utf-8")
    print(f"  wrote {OUTPUT_FILE.name} ({len(rendered)} bytes, {len(in_window)} items)")


def main() -> int:
    print("=" * 60)
    print(f"Editorials aggregator run @ {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    scraped: list[Article] = []
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
            scraped.extend(items)
        except Exception as e:
            print(f"  ✗ [{name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            failures.append(f"{name}: {type(e).__name__}")

    print()
    print(f"=== Scraped: {len(scraped)} articles "
          f"across {len(SCRAPERS) - len(failures)}/{len(SCRAPERS)} streams")
    if failures:
        print(f"=== Failures: {', '.join(failures)}")

    # Filter out stub / not-really-an-article entries by body length.
    filtered = [a for a in scraped if a.body_chars >= MIN_BODY_CHARS]
    dropped = len(scraped) - len(filtered)
    if dropped:
        print(f"=== Filtered: dropped {dropped} item(s) below {MIN_BODY_CHARS}-char floor")

    # Merge new items with existing editorials.md (URL-dedup), sort, write back.
    write_markdown(filtered)

    # Exit non-zero only if every stream failed — partial success is OK.
    if failures and len(failures) == len(SCRAPERS):
        print("ERROR: all streams failed — bailing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
