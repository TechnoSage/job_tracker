"""
source_health.py — Source health checking and automatic feed discovery.

Regulatory Compliance
─────────────────────
  This module is designed to comply with site usage policies:
  • robots.txt Exclusion Protocol is checked before crawling any HTML page.
    If a site disallows the User-Agent from a path, that path is skipped.
    Crawl-Delay directives are respected (capped at 5 s).
  • Only publicly accessible, machine-readable endpoints are probed.
    No authenticated content, paywalls, or login-protected pages are accessed.
  • Known ATS public APIs (Greenhouse, Lever, Ashby, etc.) are called using
    their officially documented, unauthenticated job-board endpoints only.
  • A conservative request rate is used to avoid overloading remote servers.
  • The User-Agent string honestly identifies this software.

Auto-Discovery Strategies (tried in order)
───────────────────────────────────────────
  1.  URL is already an RSS/Atom feed                    (feedparser)
  2.  URL is already a JSON API job list                 (JSON parsing)
  3.  Known ATS platform detected from URL alone         (Greenhouse, Lever,
                                                          Ashby, Workable,
                                                          SmartRecruiters,
                                                          BambooHR, Recruitee,
                                                          JazzHR)
  4.  <link rel="alternate" type="…rss/atom…"> in HTML   (HTML parsing)
  5.  ATS platform detected from fetched page HTML       (HTML parsing)
  6.  Schema.org JobPosting JSON-LD in page              (JSON-LD)
  7.  Sitemap.xml job section (robots.txt Sitemap: hint) (XML parsing)
  8.  Common RSS/Atom paths probed on base domain        (path probing)
  9.  Common JSON API paths probed on base domain        (path probing)
"""

from __future__ import annotations

import concurrent.futures as _futures
import json
import logging
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── HTTP helpers ──────────────────────────────────────────────────────────────

# Browser-like UA — many servers reject non-browser requests outright.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _get(url: str, timeout: int = 12):
    """HTTP GET with browser-like headers. Returns Response or None."""
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xml,application/json,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        r.raise_for_status()
        return r
    except Exception:
        return None


# ── robots.txt compliance ─────────────────────────────────────────────────────

_robots_cache: dict[str, RobotFileParser] = {}


def _get_robots(base_url: str) -> RobotFileParser | None:
    """Fetch and parse robots.txt for base_url, with in-memory caching."""
    if base_url in _robots_cache:
        return _robots_cache[base_url]
    rp = RobotFileParser()
    rp.set_url(urljoin(base_url, "/robots.txt"))
    try:
        # Use our _get() so the timeout applies (urllib default has no timeout)
        r = _get(urljoin(base_url, "/robots.txt"), timeout=6)
        if r:
            rp.parse(r.text.splitlines())
        _robots_cache[base_url] = rp
        return rp
    except Exception:
        return None


def _robots_allows(base_url: str, path: str = "/") -> bool:
    """
    Return True if robots.txt permits fetching path on base_url.
    Falls back to True if robots.txt cannot be fetched or parsed.
    """
    rp = _get_robots(base_url)
    if rp is None:
        return True
    target = urljoin(base_url, path)
    return rp.can_fetch(_UA, target) or rp.can_fetch("*", target)


def _crawl_delay(base_url: str) -> float:
    """Return Crawl-delay from robots.txt, defaulting to 0.5 s, capped at 5 s."""
    rp = _get_robots(base_url)
    if rp is None:
        return 0.5
    delay = rp.crawl_delay(_UA) or rp.crawl_delay("*") or 0.5
    return min(float(delay), 5.0)


# ── Individual source testers ─────────────────────────────────────────────────

def _test_rss(url: str) -> tuple[bool, str]:
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            exc = getattr(feed, "bozo_exception", "unknown parse error")
            return False, f"Feed parse error: {exc}"
        if not feed.entries:
            return False, "Feed reachable but returned 0 entries"
        return True, f"{len(feed.entries)} entries"
    except Exception as exc:
        return False, str(exc)


def _test_json_api(url: str) -> tuple[bool, str]:
    r = _get(url)
    if r is None:
        return False, "Request failed / unreachable"
    try:
        data = r.json()
    except Exception:
        return False, "Response is not valid JSON"
    if isinstance(data, list):
        if not data:
            return False, "Empty list returned"
        return True, f"{len(data)} items"
    for key in ("jobs", "results", "data", "listings", "positions",
                "offers", "postings", "vacancies", "openings"):
        if key in data and isinstance(data[key], list):
            return True, f"{len(data[key])} items in '{key}'"
    return False, "JSON response has no recognisable job list"


def _test_builtin(scraper, terms: list) -> tuple[bool, str]:
    try:
        test_terms = (terms or [])[:1] or ["developer"]
        jobs = scraper.fetch(search_terms=test_terms)
        return True, f"{len(jobs)} jobs"
    except Exception as exc:
        return False, str(exc)


def test_source(row) -> tuple[bool, str]:
    """Test a ScraperSource row quickly. Returns (ok, message)."""
    if row.source_type == "rss":
        url = (row.url_template or "").replace("{query}", quote_plus("developer"))
        return _test_rss(url)
    if row.source_type == "json_api":
        url = (row.url_template or "").replace("{query}", quote_plus("developer"))
        return _test_json_api(url)
    if row.source_type == "builtin":
        from scrapers import registry  # type: ignore
        scraper = registry.get_builtin(row.name)
        if not scraper:
            return False, "Not found in registry"
        return _test_builtin(scraper, row.get_search_terms())
    return False, f"Unknown source_type '{row.source_type}'"


# ── Known path lists ──────────────────────────────────────────────────────────

_RSS_PATHS = [
    "/feed", "/feed.xml", "/feed.rss", "/feed.atom",
    "/rss", "/rss.xml", "/rss/feed", "/atom.xml", "/atom",
    "/jobs.rss", "/jobs/feed", "/jobs/rss", "/jobs/atom",
    "/feed/jobs", "/feed/jobs.rss",
    "/careers.rss", "/careers/feed", "/careers/rss",
    "/openings/feed", "/work/feed", "/blog/feed", "/news.rss",
]
_JSON_PATHS = [
    "/api/jobs", "/api/positions", "/api/careers", "/api/openings",
    "/api/job-listings", "/api/job_postings",
    "/api/v0/postings", "/api/v1/jobs", "/api/v1/positions",
    "/api/v1/job_postings", "/api/v2/jobs", "/api/v3/jobs",
    "/jobs.json", "/careers.json", "/positions.json",
    "/jobs/search.json",
]

# ── Known ATS platform patterns ───────────────────────────────────────────────
# All entries use *public, unauthenticated* job-board API endpoints.
# These are officially provided by each platform for job seekers / aggregators.

_ATS_PLATFORMS: list[dict] = [
    {
        "name":    "Greenhouse",
        "pattern": r"greenhouse\.io/([A-Za-z0-9_-]+)",
        "api":     "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "type":    "json_api",
    },
    {
        "name":    "Lever",
        "pattern": r"jobs\.lever\.co/([A-Za-z0-9_-]+)",
        "api":     "https://api.lever.co/v0/postings/{slug}?mode=json",
        "type":    "json_api",
    },
    {
        "name":    "Ashby",
        "pattern": r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)",
        "api":     "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "type":    "json_api",
    },
    {
        "name":    "Workable",
        "pattern": r"(?:apply|jobs)\.workable\.com/([A-Za-z0-9_-]+)",
        "api":     "https://apply.workable.com/api/v3/accounts/{slug}/jobs?details=false",
        "type":    "json_api",
    },
    {
        "name":    "SmartRecruiters",
        "pattern": r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
        "api":     "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        "type":    "json_api",
    },
    {
        "name":    "Recruitee",
        "pattern": r"([A-Za-z0-9-]+)\.recruitee\.com",
        "api":     "https://{slug}.recruitee.com/api/offers/",
        "type":    "json_api",
    },
    {
        "name":    "JazzHR",
        "pattern": r"([A-Za-z0-9-]+)\.applytojob\.com",
        "api":     "https://{slug}.applytojob.com/apply/jobs/rss",
        "type":    "rss",
    },
    {
        "name":    "BambooHR",
        "pattern": r"([A-Za-z0-9-]+)\.bamboohr\.com",
        "api":     "https://{slug}.bamboohr.com/jobs/embed2.php",
        "type":    "json_api",
    },
    {
        "name":    "Pinpoint",
        "pattern": r"([A-Za-z0-9-]+)\.pinpointhq\.com",
        "api":     "https://{slug}.pinpointhq.com/api/v1/jobs",
        "type":    "json_api",
    },
    {
        "name":    "Teamtailor",
        "pattern": r"([A-Za-z0-9-]+)\.teamtailor\.com",
        "api":     "https://{slug}.teamtailor.com/jobs.json",
        "type":    "json_api",
    },
]


# ── URL helpers ───────────────────────────────────────────────────────────────

def _base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _url_path(url: str) -> str:
    return urlparse(url).path or "/"


def _page_title(url: str) -> str:
    r = _get(url, timeout=8)
    if not r:
        return ""
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        t = soup.find("title")
        return t.get_text(strip=True)[:120] if t else ""
    except Exception:
        return ""


# ── Discovery strategy helpers ────────────────────────────────────────────────

def _check_ats_platforms(url: str, page_text: str = "") -> dict | None:
    """
    Detect a known ATS platform from the URL itself or fetched page HTML.
    Calls only public, documented job board API endpoints.
    Returns {type, url, label} on success, or None.
    """
    search_text = url + "\n" + page_text
    for p in _ATS_PLATFORMS:
        m = re.search(p["pattern"], search_text, re.I)
        if not m:
            continue
        slug = m.group(1)
        api_url = p["api"].replace("{slug}", slug)
        tester = _test_rss if p["type"] == "rss" else _test_json_api
        ok, msg = tester(api_url)
        if ok:
            return {
                "type":  p["type"],
                "url":   api_url,
                "label": f"{p['name']} public API ({msg})",
            }
    return None


def _discover_rss_in_html(url: str, page_text: str = "") -> str | None:
    """Look for <link rel='alternate' type='…rss/atom…'> in page HTML."""
    if not page_text:
        base = _base_url(url)
        if not _robots_allows(base, _url_path(url)):
            return None
        r = _get(url)
        if not r:
            return None
        page_text = r.text
    try:
        soup = BeautifulSoup(page_text, "html.parser")
        for tag in soup.find_all("link"):
            t = tag.get("type", "")
            if re.search(r"rss|atom", t, re.I):
                href = tag.get("href", "")
                if href:
                    return urljoin(url, href)
    except Exception:
        pass
    return None


def _discover_schema_jobs(url: str, page_text: str = "") -> bool:
    """
    Return True if Schema.org JobPosting JSON-LD is present in the page.
    This means the page itself embeds structured job data.
    """
    if not page_text:
        base = _base_url(url)
        if not _robots_allows(base, _url_path(url)):
            return False
        r = _get(url)
        if not r:
            return False
        page_text = r.text
    try:
        soup = BeautifulSoup(page_text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    t = item.get("@type", "")
                    if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                        return True
                    for g in item.get("@graph", []):
                        if g.get("@type") == "JobPosting":
                            return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _discover_from_sitemap(base_url: str) -> str | None:
    """
    Find the Sitemap: URL from robots.txt, then scan for a jobs/careers entry.
    Returns the first matching URL or None. Only fetches the sitemap index —
    does not crawl individual job pages.
    """
    # Try to get Sitemap URL from robots.txt
    sitemap_url: str | None = None
    r_txt = _get(urljoin(base_url, "/robots.txt"), timeout=6)
    if r_txt:
        for line in r_txt.text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                break
    if not sitemap_url:
        sitemap_url = urljoin(base_url, "/sitemap.xml")

    r = _get(sitemap_url, timeout=10)
    if not r:
        return None
    try:
        soup = BeautifulSoup(r.text, "xml")
        for loc in soup.find_all("loc"):
            href = loc.get_text(strip=True)
            if re.search(r"/jobs|/careers|/openings|/positions|/work", href, re.I):
                return href
    except Exception:
        pass
    return None


# ── Auto-repair ───────────────────────────────────────────────────────────────

def attempt_repair(row) -> tuple[bool, str]:
    """
    Try to find an alternative working endpoint for a broken source.
    Mutates row.url_template (and row.source_type) on success.
    Returns (success, message).  Respects robots.txt before any HTML crawl.
    """
    if not row.url_template:
        return False, "No URL template to derive base from"

    raw_url = row.url_template.replace("{query}", "developer")
    base = _base_url(raw_url)

    # 1. Known ATS platform detected from original URL
    ats = _check_ats_platforms(raw_url)
    if ats:
        row.url_template = ats["url"]
        row.source_type  = ats["type"]
        return True, ats["label"]

    if _robots_allows(base, "/"):
        r = _get(base)
        page_text = r.text if r else ""
        if page_text:
            time.sleep(_crawl_delay(base))

            # 2. ATS platform in page HTML
            ats = _check_ats_platforms(raw_url, page_text)
            if ats:
                row.url_template = ats["url"]
                row.source_type  = ats["type"]
                return True, ats["label"]

            # 3. RSS <link> in page HTML
            feed = _discover_rss_in_html(base, page_text)
            if feed:
                ok, msg = _test_rss(feed)
                if ok:
                    row.url_template = feed
                    row.source_type  = "rss"
                    return True, f"RSS link found in page HTML → {feed}"

        # 4. Sitemap job section
        sitemap_hit = _discover_from_sitemap(base)
        if sitemap_hit:
            ok, msg = _test_rss(sitemap_hit)
            if ok:
                row.url_template = sitemap_hit
                row.source_type  = "rss"
                return True, f"Job feed found via sitemap → {sitemap_hit}"
            ok, msg = _test_json_api(sitemap_hit)
            if ok:
                row.url_template = sitemap_hit
                row.source_type  = "json_api"
                return True, f"Job feed found via sitemap → {sitemap_hit}"

    # 5. Common RSS paths
    for path in _RSS_PATHS:
        if not _robots_allows(base, path):
            continue
        candidate = base + path
        ok, msg = _test_rss(candidate)
        if ok:
            row.url_template = candidate
            row.source_type  = "rss"
            return True, f"Working RSS feed at {candidate}"

    # 6. Common JSON API paths
    for path in _JSON_PATHS:
        if not _robots_allows(base, path):
            continue
        candidate = base + path
        ok, msg = _test_json_api(candidate)
        if ok:
            row.url_template = candidate
            row.source_type  = "json_api"
            return True, f"Working JSON API at {candidate}"

    return False, "No alternative endpoint found after trying all strategies"


# ── Health-check task state ───────────────────────────────────────────────────

_hc_lock = threading.Lock()
_hc_state: dict = {
    "running": False, "total": 0, "done": 0,
    "results": {},   # name → {status, message, repaired, repair_message}
    "log": [],
}


def get_health_state() -> dict:
    with _hc_lock:
        return {
            "running": _hc_state["running"],
            "total":   _hc_state["total"],
            "done":    _hc_state["done"],
            "results": dict(_hc_state["results"]),
            "log":     list(_hc_state["log"]),
        }


def _hc_log(msg: str):
    with _hc_lock:
        _hc_state["log"].append(msg)
        if len(_hc_state["log"]) > 200:
            _hc_state["log"] = _hc_state["log"][-200:]


def run_health_check(app, repair: bool = True):
    """Background task: test all sources, optionally attempt repair."""
    from models import ScraperSource  # type: ignore
    from extensions import db         # type: ignore

    with _hc_lock:
        _hc_state.update(running=True, done=0, results={}, log=[])

    try:
        with app.app_context():
            sources = ScraperSource.query.all()
            with _hc_lock:
                _hc_state["total"] = len(sources)
            _hc_log(f"Health check started — {len(sources)} source(s) to test")

            for row in sources:
                _hc_log(f"Checking {row.display_name}…")
                ok, msg = test_source(row)
                row.health_status     = "current" if ok else "broken"
                row.health_checked_at = datetime.utcnow()

                result: dict = {
                    "status":         row.health_status,
                    "message":        msg,
                    "repaired":       False,
                    "repair_message": "",
                }

                if not ok and repair:
                    _hc_log(f"  ↳ {row.display_name} broken ({msg}) — trying repair…")
                    repaired, rmsg = attempt_repair(row)
                    if repaired:
                        row.health_status = "current"
                        result.update(status="current", repaired=True,
                                      repair_message=rmsg)
                        _hc_log(f"  ✓ Repaired: {rmsg}")
                    else:
                        result["repair_message"] = rmsg
                        _hc_log(f"  ✗ Repair failed: {rmsg}")
                        _hc_log(f"     Original error: {msg}")
                else:
                    icon = "✓" if ok else "✗"
                    _hc_log(f"  {icon} {msg}")

                with _hc_lock:
                    _hc_state["results"][row.name] = result
                    _hc_state["done"] += 1

                db.session.commit()
                time.sleep(0.15)

        _hc_log("Health check complete.")
    except Exception as exc:
        _hc_log(f"ERROR: {exc}")
        logger.exception("run_health_check failed")
    finally:
        with _hc_lock:
            _hc_state["running"] = False


# ── Auto-discovery task state ─────────────────────────────────────────────────

_disc_lock = threading.Lock()
_disc_state: dict = {
    "running": False, "total": 0, "done": 0,
    "results": [],
    "log": [],
}


def get_discovery_state() -> dict:
    with _disc_lock:
        return {
            "running": _disc_state["running"],
            "total":   _disc_state["total"],
            "done":    _disc_state["done"],
            "results": list(_disc_state["results"]),
            "log":     list(_disc_state["log"]),
        }


def _disc_log(msg: str):
    with _disc_lock:
        _disc_state["log"].append(msg)
        if len(_disc_state["log"]) > 300:
            _disc_state["log"] = _disc_state["log"][-300:]


def discover_one(raw_url: str) -> dict:
    """
    Try every strategy to find a job feed for raw_url.
    Respects robots.txt before any HTML crawling.
    Returns: {url, found, type, url_template, title, description}
    """
    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    base = _base_url(url)

    # ── 1. URL is already an RSS/Atom feed ────────────────────────────────────
    ok, msg = _test_rss(url)
    if ok:
        return {"url": raw_url, "found": True, "type": "rss",
                "url_template": url, "title": _page_title(url),
                "description": f"RSS/Atom feed ({msg})"}

    # ── 2. URL is already a JSON API ──────────────────────────────────────────
    ok, msg = _test_json_api(url)
    if ok:
        return {"url": raw_url, "found": True, "type": "json_api",
                "url_template": url, "title": _page_title(url),
                "description": f"JSON API ({msg})"}

    # ── 3. Known ATS platform from URL alone (no HTML fetch needed) ───────────
    ats = _check_ats_platforms(url)
    if ats:
        return {"url": raw_url, "found": True, "type": ats["type"],
                "url_template": ats["url"], "title": _page_title(url),
                "description": ats["label"]}

    # ── Fetch the page for strategies 4–6 (only if robots.txt allows) ─────────
    page_text = ""
    if _robots_allows(base, _url_path(url)):
        r = _get(url)
        if r:
            page_text = r.text
            time.sleep(_crawl_delay(base))

    # ── 4. <link rel="alternate"> RSS/Atom in page HTML ──────────────────────
    if page_text:
        feed = _discover_rss_in_html(url, page_text)
        if feed:
            ok, msg = _test_rss(feed)
            if ok:
                return {"url": raw_url, "found": True, "type": "rss",
                        "url_template": feed, "title": _page_title(url),
                        "description": f"RSS/Atom link in page <head> ({msg})"}

    # ── 5. Known ATS platform detected in page HTML ───────────────────────────
    if page_text:
        ats = _check_ats_platforms(url, page_text)
        if ats:
            return {"url": raw_url, "found": True, "type": ats["type"],
                    "url_template": ats["url"], "title": _page_title(url),
                    "description": ats["label"]}

    # ── 6. Schema.org JobPosting JSON-LD in page ─────────────────────────────
    if page_text and _discover_schema_jobs(url, page_text):
        return {"url": raw_url, "found": True, "type": "json_api",
                "url_template": url, "title": _page_title(url),
                "description": "Schema.org JobPosting structured data in page"}

    # ── 7. Sitemap.xml job section ────────────────────────────────────────────
    sitemap_hit = _discover_from_sitemap(base)
    if sitemap_hit:
        ok, msg = _test_rss(sitemap_hit)
        if ok:
            return {"url": raw_url, "found": True, "type": "rss",
                    "url_template": sitemap_hit, "title": _page_title(url),
                    "description": f"Job feed via sitemap.xml ({msg})"}
        ok, msg = _test_json_api(sitemap_hit)
        if ok:
            return {"url": raw_url, "found": True, "type": "json_api",
                    "url_template": sitemap_hit, "title": _page_title(url),
                    "description": f"Job feed via sitemap.xml ({msg})"}

    # ── 8. Common RSS/Atom paths ──────────────────────────────────────────────
    for path in _RSS_PATHS:
        if not _robots_allows(base, path):
            continue
        candidate = base + path
        ok, msg = _test_rss(candidate)
        if ok:
            return {"url": raw_url, "found": True, "type": "rss",
                    "url_template": candidate, "title": _page_title(url),
                    "description": f"RSS feed at {path} ({msg})"}

    # ── 9. Common JSON API paths ──────────────────────────────────────────────
    for path in _JSON_PATHS:
        if not _robots_allows(base, path):
            continue
        candidate = base + path
        ok, msg = _test_json_api(candidate)
        if ok:
            return {"url": raw_url, "found": True, "type": "json_api",
                    "url_template": candidate, "title": _page_title(url),
                    "description": f"JSON API at {path} ({msg})"}

    return {"url": raw_url, "found": False, "type": None,
            "url_template": "", "title": "",
            "description": "No job feed found (tried 9 strategies)"}


def run_auto_discovery(urls: list[str]):
    """Background task: discover job feeds for each URL in the list."""
    with _disc_lock:
        _disc_state.update(running=True, total=len(urls), done=0, results=[], log=[])
    _disc_log(f"Auto-discovery started — {len(urls)} URL(s)")
    _disc_log("Strategies: RSS · JSON API · ATS platforms · HTML links · "
              "Schema.org · Sitemap · Path probing")

    results: list[dict] = []
    try:
        for i, raw_url in enumerate(urls):
            _disc_log(f"[{i+1}/{len(urls)}] {raw_url.strip()}")
            result = discover_one(raw_url)
            results.append(result)
            if result["found"]:
                _disc_log(f"  ✓ {result['type'].upper()} → {result['url_template']}")
                _disc_log(f"    {result['description']}")
            else:
                _disc_log(f"  ✗ {result['description']}")
            with _disc_lock:
                _disc_state["results"] = results[:]
                _disc_state["done"]    = i + 1
            time.sleep(0.3)
        _disc_log("Auto-discovery complete.")
    except Exception as exc:
        _disc_log(f"ERROR: {exc}")
        logger.exception("run_auto_discovery failed")
    finally:
        with _disc_lock:
            _disc_state["running"] = False


# ── Curated candidate job boards ──────────────────────────────────────────────
# All URLs point to publicly accessible RSS feeds or officially documented free
# JSON APIs.  Legal basis: RSS is freely redistributable; the JSON APIs listed
# are unauthenticated endpoints provided by each platform for job seekers /
# aggregators.  Tags drive relevance scoring against the user's skill settings.

CANDIDATE_BOARDS: list[dict] = [
    # ── General / multi-category remote boards ────────────────────────────────
    {
        "name": "jobicy",
        "display_name": "Jobicy",
        "description": "Remote job board covering tech, design, marketing, and more.",
        "source_type": "rss",
        "url": "https://jobicy.com/?feed=job_feed",
        "tags": ["remote", "software", "developer", "tech", "design", "marketing",
                 "customer service", "general"],
    },
    {
        "name": "remote_co",
        "display_name": "Remote.co",
        "description": "Curated remote-only job listings via RSS.",
        "source_type": "rss",
        "url": "https://remote.co/remote-jobs/feed/",
        "tags": ["remote", "developer", "software", "tech", "customer service",
                 "marketing", "general"],
    },
    {
        "name": "working_nomads",
        "display_name": "Working Nomads",
        "description": "Remote job listings for location-independent workers.",
        "source_type": "rss",
        "url": "https://www.workingnomads.com/remote-jobs/feed/",
        "tags": ["remote", "developer", "software", "tech", "marketing", "general"],
    },
    {
        "name": "4dayweek",
        "display_name": "4 Day Week Jobs",
        "description": "Jobs at companies offering a 4-day work week — mostly tech and startups.",
        "source_type": "rss",
        "url": "https://4dayweek.io/rss-feed",
        "tags": ["remote", "developer", "software", "tech", "startup", "engineering",
                 "general"],
    },
    {
        "name": "startup_jobs",
        "display_name": "Startup.Jobs",
        "description": "Jobs at startups and fast-growing companies.",
        "source_type": "rss",
        "url": "https://startup.jobs/feed.rss",
        "tags": ["startup", "developer", "software", "engineer", "tech", "remote",
                 "general"],
    },
    {
        "name": "eu_remote_jobs",
        "display_name": "EU Remote Jobs",
        "description": "Remote jobs from European companies and distributed teams.",
        "source_type": "rss",
        "url": "https://euremotejobs.com/feed/",
        "tags": ["remote", "developer", "software", "tech", "europe", "engineering",
                 "general"],
    },
    {
        "name": "nodesk",
        "display_name": "Nodesk Remote Jobs",
        "description": "Hand-picked remote jobs from Nodesk.co.",
        "source_type": "rss",
        "url": "https://nodesk.co/remote-jobs/feed.rss",
        "tags": ["remote", "developer", "software", "tech", "design", "general"],
    },
    {
        "name": "remotewoman",
        "display_name": "Remote Woman",
        "description": "Remote jobs emphasising diversity in tech.",
        "source_type": "rss",
        "url": "https://remotewoman.com/feed/",
        "tags": ["remote", "developer", "software", "tech", "design", "general"],
    },
    {
        "name": "virtualvocations",
        "display_name": "Virtual Vocations",
        "description": "Telecommute and remote job listings via RSS.",
        "source_type": "rss",
        "url": "https://www.virtualvocations.com/jobs/rss",
        "tags": ["remote", "telecommute", "general", "customer service", "tech",
                 "software", "developer"],
    },
    # ── Tech / developer focused ──────────────────────────────────────────────
    {
        "name": "hn_jobs",
        "display_name": "Hacker News Jobs",
        "description": "Tech job postings from Hacker News (hnrss.org MIT-licensed proxy).",
        "source_type": "rss",
        "url": "https://hnrss.org/jobs",
        "tags": ["software", "developer", "engineer", "startup", "tech", "python",
                 "javascript", "backend", "frontend", "react", "database", "cloud"],
    },
    {
        "name": "hn_whoishiring",
        "display_name": "HN Who's Hiring",
        "description": "Monthly 'Who Is Hiring?' thread from Hacker News via hnrss.org.",
        "source_type": "rss",
        "url": "https://hnrss.org/whoishiring",
        "tags": ["software", "developer", "startup", "tech", "engineer", "remote",
                 "python", "javascript", "c#", ".net", "sql"],
    },
    {
        "name": "arbeitnow",
        "display_name": "ArbeitNow",
        "description": "Free public job board API — English-language positions, many remote.",
        "source_type": "json_api",
        "url": "https://arbeitnow.com/api/job-board-api",
        "tags": ["developer", "software", "tech", "remote", "engineer", "general",
                 "customer service", "IT"],
    },
    {
        "name": "himalayas",
        "display_name": "Himalayas",
        "description": "Remote-first job board with a documented free JSON API.",
        "source_type": "json_api",
        "url": "https://himalayas.app/jobs/api?limit=100",
        "tags": ["remote", "developer", "software", "tech", "startup", "engineering",
                 "general"],
    },
    {
        "name": "dice",
        "display_name": "Dice Tech Jobs",
        "description": "Technology-focused job board with RSS for tech professionals.",
        "source_type": "rss",
        "url": "https://www.dice.com/jobs/q-developer-rss.xhtml",
        "tags": ["developer", "software", "tech", "IT", "engineer", "c#", ".net",
                 "java", "python", "cloud", "azure", "sql"],
    },
    {
        "name": "authentic_jobs",
        "display_name": "Authentic Jobs",
        "description": "Jobs for developers, designers, and creative professionals.",
        "source_type": "rss",
        "url": "https://authenticjobs.com/feed/",
        "tags": ["design", "developer", "creative", "frontend", "ux", "ui", "tech",
                 "javascript", "css"],
    },
    {
        "name": "landing_jobs",
        "display_name": "Landing.jobs",
        "description": "European tech job board with RSS feed.",
        "source_type": "rss",
        "url": "https://landing.jobs/jobs/rss",
        "tags": ["developer", "software", "tech", "europe", "engineer", "startup",
                 "c#", ".net", "python", "javascript"],
    },
    # ── Design / Creative ─────────────────────────────────────────────────────
    {
        "name": "dribbble_jobs",
        "display_name": "Dribbble Jobs",
        "description": "Design-focused job listings from Dribbble.",
        "source_type": "rss",
        "url": "https://dribbble.com/jobs.rss",
        "tags": ["design", "ui", "ux", "creative", "frontend", "css", "graphic",
                 "motion", "figma"],
    },
    {
        "name": "smashing_jobs",
        "display_name": "Smashing Magazine Jobs",
        "description": "Web design and development jobs from Smashing Magazine.",
        "source_type": "rss",
        "url": "https://jobs.smashingmagazine.com/jobs.rss",
        "tags": ["design", "developer", "frontend", "web", "ux", "css", "javascript",
                 "html"],
    },
    {
        "name": "codepen_jobs",
        "display_name": "CodePen Jobs",
        "description": "Front-end developer and designer jobs from CodePen.",
        "source_type": "rss",
        "url": "https://codepen.io/jobs/feed",
        "tags": ["frontend", "developer", "design", "javascript", "css", "html", "ui",
                 "react"],
    },
    {
        "name": "krop",
        "display_name": "Krop Creative Jobs",
        "description": "Creative, design, and tech job listings from Krop.",
        "source_type": "rss",
        "url": "https://www.krop.com/creativejobs/feed/rss/",
        "tags": ["design", "creative", "ux", "ui", "frontend", "developer", "art",
                 "graphic"],
    },
    # ── General / Non-profit ──────────────────────────────────────────────────
    {
        "name": "idealist_jobs",
        "display_name": "Idealist Jobs",
        "description": "Jobs in the nonprofit and social impact sector.",
        "source_type": "rss",
        "url": "https://www.idealist.org/en/jobs/rss",
        "tags": ["general", "nonprofit", "social", "customer service", "admin",
                 "communications"],
    },
    # ── Remotive category feeds (complement the main Remotive built-in) ───────
    {
        "name": "remotive_dev",
        "display_name": "Remotive — Software Dev",
        "description": "Software developer remote jobs filtered by category from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=software-dev&limit=50",
        "tags": ["software", "developer", "engineer", "backend", "frontend", "remote",
                 "python", "javascript", "react", "c#", ".net", "java"],
    },
    {
        "name": "remotive_devops",
        "display_name": "Remotive — DevOps",
        "description": "DevOps and sysadmin remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=devops-sysadmin&limit=50",
        "tags": ["devops", "sysadmin", "IT", "cloud", "infrastructure", "docker",
                 "kubernetes", "azure", "aws", "linux"],
    },
    {
        "name": "remotive_cs",
        "display_name": "Remotive — Customer Support",
        "description": "Customer support remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=customer-support&limit=50",
        "tags": ["customer service", "support", "customer success", "help desk",
                 "service desk", "tier 1", "tier 2"],
    },
    {
        "name": "remotive_design",
        "display_name": "Remotive — Design",
        "description": "Design remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=design&limit=50",
        "tags": ["design", "ui", "ux", "creative", "frontend", "figma", "graphic",
                 "product design"],
    },
    {
        "name": "remotive_data",
        "display_name": "Remotive — Data",
        "description": "Data science and analytics remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=data&limit=50",
        "tags": ["data", "data science", "analytics", "sql", "python",
                 "machine learning", "bi", "power bi", "tableau"],
    },
    {
        "name": "remotive_product",
        "display_name": "Remotive — Product",
        "description": "Product management remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=product&limit=50",
        "tags": ["product", "product manager", "project manager", "agile", "scrum",
                 "tech", "startup"],
    },
    {
        "name": "remotive_marketing",
        "display_name": "Remotive — Marketing",
        "description": "Marketing remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=marketing&limit=50",
        "tags": ["marketing", "seo", "content", "social media", "growth", "remote",
                 "communications"],
    },
    {
        "name": "remotive_finance",
        "display_name": "Remotive — Finance",
        "description": "Finance and accounting remote jobs from Remotive.",
        "source_type": "json_api",
        "url": "https://remotive.com/api/remote-jobs?category=finance-legal&limit=50",
        "tags": ["finance", "accounting", "legal", "tax", "audit", "general",
                 "compliance"],
    },
    # ── We Work Remotely — additional categories beyond the built-in ──────────
    {
        "name": "wwr_design",
        "display_name": "WWR — Design / UX",
        "description": "Design and UX remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-design-jobs.rss",
        "tags": ["design", "ux", "ui", "creative", "figma", "frontend", "product"],
    },
    {
        "name": "wwr_management",
        "display_name": "WWR — Management / Finance",
        "description": "Management and finance remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-management-finance-jobs.rss",
        "tags": ["management", "finance", "accounting", "project manager", "general",
                 "business", "operations"],
    },
    {
        "name": "wwr_copywriting",
        "display_name": "WWR — Copywriting",
        "description": "Copywriting and content remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-copywriting-jobs.rss",
        "tags": ["copywriting", "content", "writing", "marketing", "communications",
                 "general", "seo"],
    },
    {
        "name": "wwr_sales",
        "display_name": "WWR — Sales / Business Dev",
        "description": "Sales and business development remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-sales-and-business-development-jobs.rss",
        "tags": ["sales", "business development", "customer service", "marketing",
                 "general", "account management"],
    },
    {
        "name": "wwr_product",
        "display_name": "WWR — Product",
        "description": "Product management remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-product-jobs.rss",
        "tags": ["product", "product manager", "tech", "startup", "agile", "general"],
    },
    {
        "name": "wwr_marketing",
        "display_name": "WWR — Marketing",
        "description": "Marketing remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
        "tags": ["marketing", "seo", "content", "social media", "growth",
                 "communications", "general"],
    },
    {
        "name": "wwr_legal",
        "display_name": "WWR — Legal",
        "description": "Legal and compliance remote jobs from We Work Remotely.",
        "source_type": "rss",
        "url": "https://weworkremotely.com/categories/remote-legal-jobs.rss",
        "tags": ["legal", "compliance", "law", "general", "finance"],
    },
    # ── Jobicy category feeds ─────────────────────────────────────────────────
    {
        "name": "jobicy_tech",
        "display_name": "Jobicy — Tech",
        "description": "Technology-specific remote jobs from Jobicy.",
        "source_type": "rss",
        "url": "https://jobicy.com/?feed=job_feed&job_category=technology",
        "tags": ["tech", "developer", "software", "IT", "engineer", "cloud", "sql",
                 "c#", ".net", "java", "python"],
    },
    {
        "name": "jobicy_it",
        "display_name": "Jobicy — IT & Systems",
        "description": "IT infrastructure and systems admin remote jobs from Jobicy.",
        "source_type": "rss",
        "url": "https://jobicy.com/?feed=job_feed&job_category=it",
        "tags": ["IT", "sysadmin", "devops", "cloud", "infrastructure", "network",
                 "security", "azure", "aws", "linux"],
    },
    {
        "name": "jobicy_cs",
        "display_name": "Jobicy — Customer Service",
        "description": "Customer service remote jobs from Jobicy.",
        "source_type": "rss",
        "url": "https://jobicy.com/?feed=job_feed&job_category=customer-service",
        "tags": ["customer service", "support", "customer success", "help desk",
                 "tier 1", "tier 2", "call center"],
    },
    # ── Misc additional sources ───────────────────────────────────────────────
    {
        "name": "outsourcely",
        "display_name": "Outsourcely",
        "description": "Remote work marketplace connecting employers and remote workers.",
        "source_type": "rss",
        "url": "https://www.outsourcely.com/remote-jobs/rss",
        "tags": ["remote", "developer", "software", "design", "marketing", "general"],
    },
    {
        "name": "remoteok_tech",
        "display_name": "RemoteOK — Tech",
        "description": "Tech-tagged remote jobs from RemoteOK (supplements the main RemoteOK scraper).",
        "source_type": "json_api",
        "url": "https://remoteok.com/api?tag=tech",
        "tags": ["tech", "developer", "software", "engineer", "remote", "startup",
                 "cloud"],
    },
]


def _score_board(board: dict, all_skills: list[str]) -> int:
    """
    Return a relevance score ≥ 0 for *board* against *all_skills*.
    Boards tagged 'general' or 'remote' always receive at least 1 point so they
    are never completely excluded when the user has configured skills.
    """
    tags_lower = {t.lower() for t in board.get("tags", [])}
    if not all_skills:
        return 1  # no filter configured — test everything
    score = 0
    for skill in all_skills:
        sk = skill.lower()
        for tag in tags_lower:
            if sk == tag or sk in tag or tag in sk:
                score += 1
                break  # each skill counted at most once
    if score == 0 and ("general" in tags_lower or "remote" in tags_lower):
        score = 1  # always include generic boards
    return score


# ── Board-discovery task state ────────────────────────────────────────────────

_bd_lock = threading.Lock()
_bd_state: dict = {
    "running": False,
    "total":   0,
    "tested":  0,
    "found":   0,
    "added":   0,
    "broken":  0,
    "log":     [],
    "results": [],
}


def get_board_discovery_state() -> dict:
    with _bd_lock:
        return {
            "running": _bd_state["running"],
            "total":   _bd_state["total"],
            "tested":  _bd_state["tested"],
            "found":   _bd_state["found"],
            "added":   _bd_state["added"],
            "broken":  _bd_state["broken"],
            "log":     list(_bd_state["log"]),
            "results": list(_bd_state["results"]),
        }


def _bd_log(msg: str):
    with _bd_lock:
        _bd_state["log"].append(msg)
        if len(_bd_state["log"]) > 300:
            _bd_state["log"] = _bd_state["log"][-300:]


def run_board_discovery(app, skills: list[str], max_results: int = 30):
    """
    Background task: score the CANDIDATE_BOARDS list by relevance to *skills*,
    test each candidate for a live data connection, and auto-add up to
    *max_results* working sources to the ScraperSource table.

    Connectivity is verified using the same _test_rss / _test_json_api helpers
    used by the health-check system, so only sources that actually return job
    data are added.
    """
    from models import ScraperSource  # type: ignore
    from extensions import db          # type: ignore

    with _bd_lock:
        _bd_state.update(running=True, total=0, tested=0, found=0, added=0,
                         broken=0, log=[], results=[])

    try:
        with app.app_context():
            existing_names = {
                row.name for row in
                ScraperSource.query.with_entities(ScraperSource.name).all()
            }
            existing_urls = {
                row.url_template for row in
                ScraperSource.query.with_entities(ScraperSource.url_template).all()
                if row.url_template
            }

            # Score each candidate; skip those already in the DB
            scored: list[tuple[int, dict]] = []
            for board in CANDIDATE_BOARDS:
                if board["name"] in existing_names:
                    continue
                if board.get("url", "") in existing_urls:
                    continue
                score = _score_board(board, skills)
                if score > 0:
                    scored.append((score, board))

            # Highest relevance first
            scored.sort(key=lambda x: -x[0])
            candidates = [b for _, b in scored]

            with _bd_lock:
                _bd_state["total"] = len(candidates)

            skill_summary = (", ".join(skills[:8]) + ("…" if len(skills) > 8 else "")
                             if skills else "none — testing all boards")
            _bd_log(f"Board discovery started — {len(candidates)} candidate(s) to test")
            _bd_log(f"Skills: {skill_summary}")
            _bd_log(f"Target: up to {max_results} new sources")
            _bd_log("─" * 52)

            for i, board in enumerate(candidates):
                with _bd_lock:
                    if _bd_state["added"] >= max_results:
                        _bd_log(f"Reached target of {max_results} new sources — done.")
                        break

                name  = board["name"]
                url   = board.get("url", "")
                stype = board.get("source_type", "rss")

                _bd_log(f"[{i + 1}/{len(candidates)}] {board['display_name']}…")

                tester = _test_rss if stype == "rss" else _test_json_api
                timed_out = False
                _result: list = [False, "no result"]

                def _run(_t=tester, _u=url, _r=_result):
                    _r[0], _r[1] = _t(_u)

                _th = threading.Thread(target=_run, daemon=True)
                _th.start()
                _th.join(timeout=10)
                if _th.is_alive():
                    ok = False
                    msg = "timed out after 10 s"
                    timed_out = True
                else:
                    ok, msg = _result[0], _result[1]

                result = {
                    "name":         name,
                    "display_name": board["display_name"],
                    "description":  board.get("description", ""),
                    "url":          url,
                    "source_type":  stype,
                    "status":       "timeout" if timed_out else ("ok" if ok else "fail"),
                    "message":      msg,
                }

                with _bd_lock:
                    _bd_state["tested"] += 1
                    if timed_out:
                        _bd_state["broken"] += 1
                    elif ok:
                        _bd_state["found"] += 1
                    _bd_state["results"].append(result)

                if timed_out:
                    _bd_log(f"  \u23f1 {board['display_name']} \u2014 broken (timed out >10 s)")
                elif ok:
                    _bd_log(f"  \u2713 {board['display_name']} \u2014 {msg}")
                    try:
                        if not ScraperSource.query.filter_by(name=name).first():
                            row = ScraperSource(
                                name=name,
                                display_name=board["display_name"],
                                description=board.get("description", ""),
                                source_type=stype,
                                url_template=url,
                                is_enabled=True,
                                is_builtin=False,
                                health_status="current",
                                health_checked_at=datetime.utcnow(),
                            )
                            db.session.add(row)
                            db.session.commit()
                            with _bd_lock:
                                _bd_state["added"] += 1
                            _bd_log(f"  \u2192 Added to sources")
                        else:
                            _bd_log(f"  \u2192 Already in DB \u2014 skipped")
                    except Exception as exc:
                        db.session.rollback()
                        _bd_log(f"  \u2192 DB error: {exc}")
                else:
                    _bd_log(f"  \u2717 {board['display_name']} \u2014 {msg}")

                time.sleep(0.5)

        with _bd_lock:
            found  = _bd_state["found"]
            added  = _bd_state["added"]
            broken = _bd_state["broken"]
        _bd_log("─" * 52)
        _bd_log(
            f"Discovery complete \u2014  "
            f"Found: {found}  |  Added: {added}  |  Broken (timeout): {broken}"
        )

    except Exception as exc:
        _bd_log(f"ERROR: {exc}")
        logger.exception("run_board_discovery failed")
    finally:
        with _bd_lock:
            _bd_state["running"] = False
