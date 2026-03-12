"""
scrapers.py — Extensible job-scraper registry.

Built-in scrapers (all free and legal)
───────────────────────────────────────
  • RemoteOK       — remoteok.com/api              (JSON API, free, no key)
  • Remotive       — remotive.com/api/remote-jobs  (JSON API, free, no key)
  • We Work Remotely — weworkremotely.com          (RSS, free, no key)
  • LinkedIn       — public guest-search HTML      (no login required)
  • USAJobs        — data.usajobs.gov/api          (official U.S. federal API, free key)
  • CareerOneStop  — api.careeronestop.org         (official DOL API, free key)

Official API keys required (free to register):
  • USAJobs:       https://developer.usajobs.gov/
                   Set USAJOBS_API_KEY + USAJOBS_USER_AGENT in .env
  • CareerOneStop: https://www.careeronestop.org/Developers/WebAPI/
                   Set CAREERONESTOP_USER_ID + CAREERONESTOP_API_KEY in .env

Unavailable sources (see UNAVAILABLE_SOURCES at module bottom)
──────────────────────────────────────────────────────────────
  • Indeed        — RSS feed discontinued Mar 2026; no free public API; ToS prohibits scraping
  • ZipRecruiter  — RSS API deprecated Mar 2025; HTML blocked by Cloudflare
  • O*NET         — Occupational database, not a job board (skill-matching API, no job listings)
  • JobRight AI   — Closed system; robots.txt blocks all crawlers from /jobs/; no API

Adding a new scraper
────────────────────
  Option A — Built-in (Python):
    1. Subclass JobScraper
    2. Set class attributes: name, display_name, description, source_type
    3. Implement fetch(search_terms) → list[dict]
    4. Call  registry.register_builtin(MyNewScraper())  at the bottom of this file.

  Option B — Custom RSS (no code):
    Add a source via the /scrapers UI in the app.
    Set type='rss' and provide a URL template.
    Use {query} as a placeholder for the search term (URL-encoded automatically).
    Example:  https://example.com/jobs.rss?q={query}&remote=true

  Option C — Custom JSON API (no code):
    Same as B but set type='json_api'.
    The generic scraper walks the top-level list and maps common field names.
"""

import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

# lxml is optional — fall back to the built-in html.parser if not installed
try:
    import lxml  # noqa: F401
    _BS4_PARSER = "lxml"
except ImportError:
    _BS4_PARSER = "html.parser"

logger = logging.getLogger(__name__)

# Rotate user-agents to reduce scraping blocks
_USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
]
_ua_index = 0


def _next_ua() -> str:
    global _ua_index
    ua = _USER_AGENTS[_ua_index % len(_USER_AGENTS)]
    _ua_index += 1
    return ua


def _headers(referer: str = "") -> dict:
    h = {
        "User-Agent": _next_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    return h


def _safe_get(url: str, referer: str = "", **kwargs):
    try:
        resp = requests.get(url, headers=_headers(referer), timeout=15, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.warning("HTTP %s for %s", code, url)
    except Exception as exc:
        logger.warning("GET %s failed: %s", url, exc)
    return None


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    return BeautifulSoup(html_text, _BS4_PARSER).get_text(separator=" ", strip=True)


def _get_entry_text(entry) -> str:
    """
    Return the richest available text from a feedparser RSS entry.
    Prefers `content` (full body, e.g. <content:encoded>) over `summary`
    so that employment-type keywords buried in the full posting are found.
    """
    content_list = entry.get("content", [])
    if content_list:
        full = " ".join(c.get("value", "") for c in content_list if c.get("value"))
        if full:
            return _strip_html(full)
    return _strip_html(entry.get("summary", ""))


def _parse_date(date_str: str):
    if not date_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%d",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=None)
        except Exception:
            pass
    return None


# ── Company HQ info lookup ───────────────────────────────────────────────────
# In-memory cache: company name (lowercased) → {"company_address": ..., "company_phone": ...}
_company_info_cache: dict = {}

_PHONE_RE = re.compile(
    r'(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}'
)


def lookup_company_info(company_name: str) -> dict:
    """Scrape DuckDuckGo instant-answer API for a company's HQ address and phone.

    Results are cached by company name for the lifetime of the process so the
    same company is only looked up once per scan run.

    Returns a dict with keys ``company_address`` and ``company_phone``
    (empty strings when not found).
    """
    empty = {"company_address": "", "company_phone": ""}
    if not company_name or not company_name.strip():
        return empty

    cache_key = company_name.lower().strip()
    if cache_key in _company_info_cache:
        return _company_info_cache[cache_key]

    result = {"company_address": "", "company_phone": ""}
    try:
        url = (
            "https://api.duckduckgo.com/"
            f"?q={quote_plus(company_name + ' headquarters')}"
            "&format=json&no_html=1&skip_disambig=1"
        )
        resp = requests.get(url, headers=_headers(), timeout=10)
        data = resp.json()

        # --- Infobox (best source: structured entity data) ---
        infobox = data.get("Infobox") or {}
        for item in infobox.get("content", []):
            label = (item.get("label") or "").lower()
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            if not result["company_address"] and label in (
                "headquarters", "hq", "address", "location", "founded", "office"
            ):
                result["company_address"] = value
            if not result["company_phone"] and label in (
                "phone", "telephone", "phone number", "contact"
            ):
                result["company_phone"] = value

        # --- AbstractText fallback: scan for phone patterns ---
        if not result["company_phone"]:
            abstract = data.get("AbstractText") or ""
            m = _PHONE_RE.search(abstract)
            if m:
                result["company_phone"] = m.group().strip()

        # --- RelatedTopics: look for an address in the first snippet ---
        if not result["company_address"]:
            for topic in (data.get("RelatedTopics") or [])[:3]:
                text = (topic.get("Text") or "")
                # Simple heuristic: look for a line that contains a ZIP / postcode
                for seg in text.split("."):
                    if re.search(r'\b\d{5}(?:-\d{4})?\b', seg):
                        result["company_address"] = seg.strip()
                        break
                if result["company_address"]:
                    break

    except Exception as exc:
        logger.debug("Company info lookup failed for '%s': %s", company_name, exc)

    _company_info_cache[cache_key] = result
    return result


# ── Normalised job dict schema ───────────────────────────────────────────────
# {
#   external_id: str,      — unique key: source_name + "_" + hash/id
#   title:       str,
#   company:     str,
#   location:    str,
#   description: str,
#   tags:        list[str],
#   salary_range:str,
#   url:         str,
#   source:      str,      — matches ScraperSource.name
#   posted_date: datetime | None,
# }
# ────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# Base class
# ══════════════════════════════════════════════════════════════════════════════

class JobScraper:
    """
    Base class for all job scrapers.
    Subclass, set class attributes, implement fetch(), register at bottom of file.
    """
    name: str = ""
    display_name: str = ""
    description: str = ""
    source_type: str = "builtin"
    default_search_terms: list = [
        "C# developer",
        ".NET developer",
        "Oracle developer remote",
        "SQL Server developer",
    ]

    def fetch(self, search_terms: list = None) -> list:
        raise NotImplementedError

    def _search_terms(self, override=None) -> list:
        return override if override else self.default_search_terms


# ══════════════════════════════════════════════════════════════════════════════
# Built-in scrapers
# ══════════════════════════════════════════════════════════════════════════════

class RemoteOKScraper(JobScraper):
    name = "remoteok"
    display_name = "RemoteOK"
    description = "Free JSON API — remote-only jobs across all categories."

    def fetch(self, search_terms=None):
        resp = _safe_get("https://remoteok.com/api")
        if not resp:
            return []
        try:
            data = resp.json()
            if isinstance(data, list) and data:
                data = data[1:]   # skip legal notice dict
        except Exception:
            return []

        jobs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            description = _strip_html(item.get("description", ""))
            tags = item.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            posted = None
            epoch = item.get("epoch")
            if epoch:
                try:
                    posted = datetime.fromtimestamp(int(epoch))
                except Exception:
                    pass

            jobs.append({
                "external_id": f"remoteok_{item.get('id', hash(item.get('url', '')))}",
                "title": item.get("position", ""),
                "company": item.get("company", ""),
                "location": item.get("location", "Remote"),
                "description": description,
                "tags": tags,
                "salary_range": item.get("salary", ""),
                "url": item.get("url", ""),
                "source": self.name,
                "posted_date": posted,
            })
        logger.info("RemoteOK: %d jobs", len(jobs))
        return jobs


class RemotiveScraper(JobScraper):
    name = "remotive"
    display_name = "Remotive"
    description = "Free JSON API — curated remote jobs in software, support & devops."
    _CATEGORIES = [
        "software-dev", "customer-support", "devops-sysadmin", "data", "qa",
    ]

    def fetch(self, search_terms=None):
        jobs = []
        seen = set()

        for cat in self._CATEGORIES:
            url = f"https://remotive.com/api/remote-jobs?category={cat}&limit=50"
            resp = _safe_get(url)
            if not resp:
                continue
            try:
                data = resp.json().get("jobs", [])
            except Exception:
                continue

            for item in data:
                ext_id = f"remotive_{item.get('id', '')}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                description = _strip_html(item.get("description", ""))
                tags = item.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                # Remotive API provides job_type ("full_time", "contract", etc.)
                # Include it as a tag so duration detection can use it.
                job_type = item.get("job_type", "")
                if job_type and job_type not in ("other", ""):
                    tags = list(tags) + [job_type.replace("_", " ")]

                jobs.append({
                    "external_id": ext_id,
                    "title": item.get("title", ""),
                    "company": item.get("company_name", ""),
                    "location": item.get("candidate_required_location", "Remote"),
                    "description": description,
                    "tags": tags,
                    "salary_range": item.get("salary", ""),
                    "url": item.get("url", ""),
                    "source": self.name,
                    "posted_date": _parse_date(item.get("publication_date", "")),
                })

            time.sleep(0.4)

        logger.info("Remotive: %d jobs", len(jobs))
        return jobs


class WWRScraper(JobScraper):
    name = "wwr"
    display_name = "We Work Remotely"
    description = "RSS feeds from weworkremotely.com — curated remote positions."
    _FEEDS = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    ]

    def fetch(self, search_terms=None):
        jobs = []
        for feed_url in self._FEEDS:
            try:
                feed = feedparser.parse(feed_url)
            except Exception as exc:
                logger.warning("WWR RSS failed (%s): %s", feed_url, exc)
                continue

            for entry in feed.entries:
                link = entry.get("link", "")
                raw_title = entry.get("title", "")
                if ":" in raw_title:
                    parts = raw_title.split(":", 1)
                    company, title = parts[0].strip(), parts[1].strip()
                else:
                    title, company = raw_title, ""

                jobs.append({
                    "external_id": f"wwr_{hash(link)}",
                    "title": title,
                    "company": company,
                    "location": "Remote",
                    "description": _get_entry_text(entry),
                    "tags": [],
                    "salary_range": "",
                    "url": link,
                    "source": self.name,
                    "posted_date": _parse_date(entry.get("published", "")),
                })
            time.sleep(0.3)

        logger.info("WWR: %d jobs", len(jobs))
        return jobs


class USAJobsScraper(JobScraper):
    """
    USAJobs.gov — Official U.S. Federal Government Jobs API.

    100% free and legal — provided by the U.S. Office of Personnel Management.
    Returns real federal job listings. No scraping; uses the official REST API.

    Setup (one-time):
    1. Register for a free API key at https://developer.usajobs.gov/
    2. Add to your .env file in the project root:
          USAJOBS_API_KEY=your_api_key_here
          USAJOBS_USER_AGENT=your@email.com
    Without these values the scraper logs a warning and returns no results.
    """
    name = "usajobs"
    display_name = "USAJobs (Federal)"
    description = (
        "Official U.S. Federal Government job listings — free, legal REST API. "
        "Requires a free API key from developer.usajobs.gov. "
        "Set USAJOBS_API_KEY and USAJOBS_USER_AGENT in your .env file."
    )
    requires_keys = True
    registration_url = "https://developer.usajobs.gov/"
    key_fields = [
        {
            "env_var": "USAJOBS_API_KEY",
            "label": "API Key",
            "placeholder": "Paste your USAJobs API key here",
        },
        {
            "env_var": "USAJOBS_USER_AGENT",
            "label": "User Agent (your email address)",
            "placeholder": "your@email.com",
        },
    ]
    default_search_terms = [
        "software developer",
        "information technology",
        "data analyst",
        "systems administrator",
        "C# developer",
        ".NET developer",
    ]
    _BASE = "https://data.usajobs.gov/api/Search"

    @staticmethod
    def test_keys(key_values):
        """Test provided keys against the USAJobs API. Returns (ok: bool, message: str)."""
        api_key    = key_values.get("USAJOBS_API_KEY", "").strip()
        user_agent = key_values.get("USAJOBS_USER_AGENT", "").strip()
        if not api_key or not user_agent:
            return False, "Both API Key and User Agent (email) are required."
        headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": user_agent,
            "Authorization-Key": api_key,
        }
        try:
            resp = requests.get(
                "https://data.usajobs.gov/api/Search",
                headers=headers,
                params={"Keyword": "developer", "ResultsPerPage": 1},
                timeout=10,
            )
            if resp.status_code == 200:
                return True, "Connection successful — API key is valid."
            elif resp.status_code == 401:
                return False, "Invalid API key (HTTP 401 Unauthorized)."
            elif resp.status_code == 403:
                return False, "Access denied (HTTP 403 Forbidden). Check your API key."
            else:
                return False, f"API returned HTTP {resp.status_code}."
        except Exception as exc:
            return False, f"Connection failed: {exc}"

    def fetch(self, search_terms=None):
        import os
        api_key    = os.environ.get("USAJOBS_API_KEY", "").strip()
        user_agent = os.environ.get("USAJOBS_USER_AGENT", "").strip()

        if not api_key or not user_agent:
            logger.warning(
                "USAJobs: USAJOBS_API_KEY and USAJOBS_USER_AGENT not set. "
                "Register at https://developer.usajobs.gov/ and add both to your .env file."
            )
            return []

        headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": user_agent,
            "Authorization-Key": api_key,
        }

        terms = self._search_terms(search_terms)
        jobs = []
        seen = set()

        for query in terms:
            try:
                params = {
                    "Keyword": query,
                    "ResultsPerPage": 25,
                    "DatePosted": 7,
                    "RemoteIndicator": "True",
                }
                resp = requests.get(self._BASE, headers=headers, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("USAJobs API failed for '%s': %s", query, exc)
                continue

            items = (data.get("SearchResult", {})
                        .get("SearchResultItems", []))

            for item in items:
                matched = item.get("MatchedObjectDescriptor", {})
                position_id = matched.get("PositionID", "")
                ext_id = f"usajobs_{position_id or hash(str(matched))}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                # Apply URL (preferred) or position URL
                apply_uris = matched.get("ApplyURI", [])
                url_val = (apply_uris[0] if apply_uris
                           else matched.get("PositionURI", ""))

                # Salary range
                salary_str = ""
                remun = matched.get("PositionRemuneration", [])
                if remun:
                    r = remun[0]
                    lo = r.get("MinimumRange", "")
                    hi = r.get("MaximumRange", "")
                    rate = r.get("RateIntervalCode", "")
                    if lo and hi:
                        try:
                            salary_str = f"${int(float(lo)):,} – ${int(float(hi)):,}"
                            if rate:
                                salary_str += f" / {rate.lower()}"
                        except Exception:
                            salary_str = f"{lo} – {hi}"

                # Location
                locations = matched.get("PositionLocation", [])
                location_str = "Remote"
                if locations:
                    loc = locations[0]
                    city = loc.get("CityName", "")
                    state = loc.get("CountrySubDivisionCode", "")
                    if city and ("anywhere" not in city.lower()
                                 and "remote" not in city.lower()):
                        location_str = f"{city}, {state}" if state else city

                # Job type tags
                schedules = matched.get("PositionSchedule", [])
                tags = [s.get("Name", "") for s in schedules if s.get("Name")]

                # Summary / description
                details = (matched.get("UserArea", {})
                                  .get("Details", {}))
                description = details.get("JobSummary", "")

                jobs.append({
                    "external_id": ext_id,
                    "title":       matched.get("PositionTitle", ""),
                    "company":     matched.get("OrganizationName",
                                              "U.S. Federal Government"),
                    "location":    location_str,
                    "description": description,
                    "tags":        tags,
                    "salary_range": salary_str,
                    "url":         url_val,
                    "source":      self.name,
                    "posted_date": _parse_date(
                        (matched.get("PublicationStartDate") or "")[:10]
                    ),
                })

            time.sleep(0.5)

        logger.info("USAJobs: %d jobs", len(jobs))
        return jobs


class CareerOneStopScraper(JobScraper):
    """
    CareerOneStop (Department of Labor) — Official U.S. job listings API.

    Free, legal, government-provided. Aggregates jobs from the National Labor
    Exchange (NLx) — a partnership between Direct Employers Association and
    the National Association of State Workforce Agencies. Broader coverage
    than USAJobs (includes private-sector employers, not just federal).

    Setup (one-time):
    1. Register at https://www.careeronestop.org/Developers/WebAPI/
    2. Add to your .env file:
          CAREERONESTOP_USER_ID=your_user_id_here
          CAREERONESTOP_API_KEY=your_api_key_here
    Without these values the scraper logs a warning and returns no results.
    """
    name = "careeronestop"
    display_name = "CareerOneStop (DOL)"
    description = (
        "U.S. Department of Labor job listings via the free CareerOneStop API. "
        "Covers private + public sector jobs from the National Labor Exchange. "
        "Set CAREERONESTOP_USER_ID and CAREERONESTOP_API_KEY in your .env file."
    )
    requires_keys = True
    registration_url = "https://www.careeronestop.org/Developers/WebAPI/"
    key_fields = [
        {
            "env_var": "CAREERONESTOP_USER_ID",
            "label": "User ID",
            "placeholder": "Your CareerOneStop User ID",
        },
        {
            "env_var": "CAREERONESTOP_API_KEY",
            "label": "API Key",
            "placeholder": "Your CareerOneStop API key (Bearer token)",
        },
    ]
    default_search_terms = [
        "software developer",
        "C# developer",
        ".NET developer",
        "Oracle developer",
        "SQL developer",
        "IT support specialist",
    ]
    _BASE = "https://api.careeronestop.org/v1/jobsearch/{user_id}/{keyword}/remote/0/0/0/0/25/0"

    @staticmethod
    def test_keys(key_values):
        """Test provided credentials against the CareerOneStop API. Returns (ok: bool, message: str)."""
        user_id = key_values.get("CAREERONESTOP_USER_ID", "").strip()
        api_key  = key_values.get("CAREERONESTOP_API_KEY", "").strip()
        if not user_id or not api_key:
            return False, "Both User ID and API Key are required."
        url = f"https://api.careeronestop.org/v1/jobsearch/{quote_plus(user_id)}/developer/remote/0/0/0/0/1/0"
        headers = {
            "Authorization": f"socApiKey {api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, params={"days": 7}, timeout=10)
            if resp.status_code == 200:
                return True, "Connection successful — credentials are valid."
            elif resp.status_code == 401:
                return False, "Invalid credentials (HTTP 401 Unauthorized)."
            elif resp.status_code == 403:
                return False, "Access denied (HTTP 403 Forbidden). Check your credentials."
            else:
                snippet = resp.text[:200] if resp.text else ""
                return False, f"API returned HTTP {resp.status_code}. {snippet}".strip()
        except Exception as exc:
            return False, f"Connection failed: {exc}"

    def fetch(self, search_terms=None):
        import os
        user_id = os.environ.get("CAREERONESTOP_USER_ID", "").strip()
        api_key  = os.environ.get("CAREERONESTOP_API_KEY", "").strip()

        if not user_id or not api_key:
            logger.warning(
                "CareerOneStop: CAREERONESTOP_USER_ID and CAREERONESTOP_API_KEY not set. "
                "Register at https://www.careeronestop.org/Developers/WebAPI/"
                " and add both to your .env file."
            )
            return []

        headers = {
            "Authorization": f"socApiKey {api_key}",
            "Content-Type": "application/json",
        }

        terms = self._search_terms(search_terms)
        jobs = []
        seen = set()

        for query in terms:
            url = self._BASE.format(
                user_id=quote_plus(user_id),
                keyword=quote_plus(query),
            )
            try:
                resp = requests.get(url, headers=headers,
                                    params={"days": 7, "enableMetaData": "true"},
                                    timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("CareerOneStop API failed for '%s': %s", query, exc)
                continue

            items = data.get("Jobs", [])
            for item in items:
                job_id = item.get("JobId", "")
                ext_id = f"careeronestop_{job_id or hash(item.get('JobURL', ''))}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                salary_str = ""
                lo = item.get("SalaryLow", "")
                hi = item.get("SalaryHigh", "")
                if lo and hi:
                    try:
                        salary_str = f"${int(float(lo)):,} – ${int(float(hi)):,}"
                    except Exception:
                        salary_str = f"{lo} – {hi}"

                jobs.append({
                    "external_id": ext_id,
                    "title":       item.get("JobTitle", ""),
                    "company":     item.get("Company", ""),
                    "location":    item.get("Location", "Remote"),
                    "description": item.get("JobSummary", ""),
                    "tags":        item.get("JobType", "").split(",") if item.get("JobType") else [],
                    "salary_range": salary_str,
                    "url":         item.get("JobURL", ""),
                    "source":      self.name,
                    "posted_date": _parse_date(item.get("DatePosted", "")),
                })

            time.sleep(0.5)

        logger.info("CareerOneStop: %d jobs", len(jobs))
        return jobs


class LinkedInScraper(JobScraper):
    """
    LinkedIn public guest-search API — no account required.

    LinkedIn's /jobs-guest/ endpoint returns paginated HTML fragments.
    A 2.5-second delay is inserted between queries to reduce rate-limiting.
    If LinkedIn blocks the requests you will simply get 0 results for that run;
    the app continues normally with other sources.
    """
    name = "linkedin"
    display_name = "LinkedIn Jobs"
    description = (
        "LinkedIn public guest-search API (no login required). "
        "A built-in delay prevents rate-limiting; occasional blocks are normal."
    )
    default_search_terms = [
        "C# developer",
        ".NET developer",
        "Oracle developer",
        "C# customer service",
        "IT support C# .NET",
    ]
    _BASE = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    def fetch(self, search_terms=None):
        terms = self._search_terms(search_terms)
        jobs = []
        seen = set()

        for query in terms:
            # f_WT=2 = remote work type
            url = (
                f"{self._BASE}"
                f"?keywords={quote_plus(query)}"
                f"&location=Remote"
                f"&f_WT=2"
                f"&start=0"
            )
            resp = _safe_get(url, referer="https://www.linkedin.com/")
            if not resp:
                time.sleep(3)
                continue

            soup = BeautifulSoup(resp.text, _BS4_PARSER)

            # LinkedIn renders job cards as <div class="base-card ...">
            cards = soup.find_all("div", class_=lambda c: c and "base-card" in c)

            for card in cards:
                # Full-link anchor
                link_tag = (
                    card.find("a", class_=lambda c: c and "base-card__full-link" in c)
                    or card.find("a", href=lambda h: h and "/jobs/view/" in h)
                )
                if not link_tag:
                    continue

                href = link_tag.get("href", "").split("?")[0]  # strip tracking params
                ext_id = f"linkedin_{hash(href)}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                title_el   = card.find(class_=lambda c: c and "base-search-card__title" in c)
                company_el = card.find(class_=lambda c: c and "base-search-card__subtitle" in c)
                location_el = card.find(class_=lambda c: c and "job-search-card__location" in c)
                date_el    = card.find("time")

                posted = None
                if date_el and date_el.get("datetime"):
                    posted = _parse_date(date_el["datetime"])

                jobs.append({
                    "external_id": ext_id,
                    "title":    title_el.get_text(strip=True)    if title_el    else "",
                    "company":  company_el.get_text(strip=True)  if company_el  else "",
                    "location": location_el.get_text(strip=True) if location_el else "Remote",
                    "description": "",   # full description requires a separate page fetch
                    "tags": [],
                    "salary_range": "",
                    "url": href,
                    "source": self.name,
                    "posted_date": posted,
                })

            logger.debug("LinkedIn '%s': %d cards", query, len(cards))
            time.sleep(2.5)   # polite rate-limiting

        logger.info("LinkedIn: %d jobs across %d queries", len(jobs), len(terms))
        return jobs


class ZipRecruiterScraper(JobScraper):
    """
    ZipRecruiter — RSS feed with JSON-LD HTML fallback.
    The RSS feed is tried first; if it returns no entries the HTML search
    results page is parsed for embedded JSON-LD JobPosting schema data.
    """
    name = "ziprecruiter"
    display_name = "ZipRecruiter"
    description = "ZipRecruiter job search via RSS feed (with HTML/JSON-LD fallback)."
    default_search_terms = [
        "C# developer",
        ".NET developer",
        "Oracle developer",
        "SQL Server developer",
        "C# customer service remote",
    ]
    _RSS_BASE    = "https://www.ziprecruiter.com/jobs/feed"
    _SEARCH_BASE = "https://www.ziprecruiter.com/candidate/search"

    def fetch(self, search_terms=None):
        terms = self._search_terms(search_terms)
        jobs = []
        seen = set()

        for query in terms:
            fetched = self._rss(query, seen)
            if not fetched:
                fetched = self._html(query, seen)
            jobs.extend(fetched)
            time.sleep(1.5)

        logger.info("ZipRecruiter: %d jobs", len(jobs))
        return jobs

    def _rss(self, query: str, seen: set) -> list:
        url = (
            f"{self._RSS_BASE}"
            f"?search={quote_plus(query)}"
            f"&location=Remote"
            f"&refine_by_location_type=only_remote"
        )
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            logger.warning("ZipRecruiter RSS failed for '%s': %s", query, exc)
            return []

        jobs = []
        for entry in feed.entries:
            link = entry.get("link", "")
            ext_id = f"ziprecruiter_{hash(link)}"
            if ext_id in seen:
                continue
            seen.add(ext_id)

            raw_title = entry.get("title", "")
            company = ""
            if " at " in raw_title:
                parts = raw_title.rsplit(" at ", 1)
                raw_title, company = parts[0].strip(), parts[1].strip()

            jobs.append({
                "external_id": ext_id,
                "title": raw_title,
                "company": company,
                "location": entry.get("location", "Remote"),
                "description": _get_entry_text(entry),
                "tags": [],
                "salary_range": "",
                "url": link,
                "source": self.name,
                "posted_date": _parse_date(entry.get("published", "")),
            })
        return jobs

    def _html(self, query: str, seen: set) -> list:
        """Parse embedded JSON-LD JobPosting objects from the search results page."""
        url = (
            f"{self._SEARCH_BASE}"
            f"?search={quote_plus(query)}"
            f"&location=Remote"
            f"&days=7"
        )
        resp = _safe_get(url, referer="https://www.ziprecruiter.com/")
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, _BS4_PARSER)
        jobs = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                    continue

                url_val = item.get("url", "")
                ext_id = f"ziprecruiter_{hash(url_val)}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                org = item.get("hiringOrganization", {})
                salary_obj = item.get("baseSalary", {})
                salary_str = ""
                if salary_obj:
                    val = salary_obj.get("value", {})
                    lo = val.get("minValue", "")
                    hi = val.get("maxValue", "")
                    if lo and hi:
                        salary_str = f"${int(lo):,} – ${int(hi):,}"

                # JSON-LD JobPosting may carry employmentType ("FULL_TIME", "PART_TIME", etc.)
                emp_type = item.get("employmentType", "")
                emp_tags = [emp_type.replace("_", " ").lower()] if emp_type else []

                jobs.append({
                    "external_id": ext_id,
                    "title": item.get("title", ""),
                    "company": org.get("name", "") if isinstance(org, dict) else "",
                    "location": "Remote",
                    "description": _strip_html(item.get("description", "")),
                    "tags": emp_tags,
                    "salary_range": salary_str,
                    "url": url_val,
                    "source": self.name,
                    "posted_date": _parse_date(item.get("datePosted", "")),
                })

        return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Generic scrapers — used for user-added custom sources
# ══════════════════════════════════════════════════════════════════════════════

class RSSFeedScraper(JobScraper):
    """
    Generic RSS/Atom scraper for custom user-added sources.
    url_template may contain {query} which is URL-encoded and substituted.
    """
    source_type = "rss"

    def __init__(self, name, display_name, url_template, description="Custom RSS feed"):
        self.name = name
        self.display_name = display_name
        self.url_template = url_template
        self.description = description

    def fetch(self, search_terms=None):
        terms = search_terms or [""]
        if "{query}" not in self.url_template:
            terms = [""]   # fetch once, no substitution

        jobs = []
        seen = set()

        for term in terms:
            url = self.url_template.replace("{query}", quote_plus(term))
            try:
                feed = feedparser.parse(url)
            except Exception as exc:
                logger.warning("Custom RSS '%s' failed: %s", self.name, exc)
                continue

            for entry in feed.entries:
                link = entry.get("link", "")
                ext_id = f"{self.name}_{hash(link)}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                raw_title = entry.get("title", "")
                company = ""
                if ":" in raw_title:
                    parts = raw_title.split(":", 1)
                    company, raw_title = parts[0].strip(), parts[1].strip()

                jobs.append({
                    "external_id": ext_id,
                    "title": raw_title,
                    "company": company,
                    "location": "Remote",
                    "description": _get_entry_text(entry),
                    "tags": [],
                    "salary_range": "",
                    "url": link,
                    "source": self.name,
                    "posted_date": _parse_date(entry.get("published", "")),
                })
            time.sleep(0.5)

        logger.info("Custom RSS '%s': %d jobs", self.name, len(jobs))
        return jobs


class JSONAPIScraper(JobScraper):
    """
    Generic JSON API scraper for custom endpoints that return a flat list.
    Tries common field-name aliases; works with most public job-board APIs.
    """
    source_type = "json_api"
    _TITLE   = ("title", "position", "job_title", "name", "role")
    _COMPANY = ("company", "company_name", "organization", "employer")
    _DESC    = ("description", "body", "content", "summary", "details")
    _URL     = ("url", "link", "apply_url", "job_url", "href")
    _SALARY  = ("salary", "compensation", "pay", "salary_range")
    _DATE    = ("date", "posted_date", "publication_date", "created_at", "date_posted")

    def __init__(self, name, display_name, url_template, description="Custom JSON API"):
        self.name = name
        self.display_name = display_name
        self.url_template = url_template
        self.description = description

    @staticmethod
    def _first(obj, keys):
        for k in keys:
            if k in obj and obj[k]:
                return str(obj[k])
        return ""

    def fetch(self, search_terms=None):
        terms = search_terms or [""]
        if "{query}" not in self.url_template:
            terms = [""]

        jobs = []
        seen = set()

        for term in terms:
            url = self.url_template.replace("{query}", quote_plus(term))
            resp = _safe_get(url)
            if not resp:
                continue

            try:
                data = resp.json()
            except Exception:
                continue

            # Accept: list, {"jobs":[...]}, {"results":[...]}, {"data":[...]}
            items = (data if isinstance(data, list)
                     else data.get("jobs",
                          data.get("results",
                          data.get("data", []))))
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                url_val = self._first(item, self._URL)
                ext_id = f"{self.name}_{hash(url_val or str(item))}"
                if ext_id in seen:
                    continue
                seen.add(ext_id)

                jobs.append({
                    "external_id": ext_id,
                    "title":       self._first(item, self._TITLE),
                    "company":     self._first(item, self._COMPANY),
                    "location":    item.get("location", "Remote"),
                    "description": _strip_html(self._first(item, self._DESC)),
                    "tags":        item.get("tags", item.get("skills", [])),
                    "salary_range":self._first(item, self._SALARY),
                    "url":         url_val,
                    "source":      self.name,
                    "posted_date": _parse_date(self._first(item, self._DATE)),
                })
            time.sleep(0.5)

        logger.info("Custom JSON API '%s': %d jobs", self.name, len(jobs))
        return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Scraper Registry
# ══════════════════════════════════════════════════════════════════════════════

class ScraperRegistry:
    """
    Central registry of all job scrapers (built-in + user-added).

    Built-in scrapers are registered at module load time via register_builtin().
    Custom scrapers (source_type rss/json_api) live only in the DB and are
    resolved dynamically on each call to fetch_all_enabled().
    """

    def __init__(self):
        self._builtins = {}

    def register_builtin(self, scraper: JobScraper):
        self._builtins[scraper.name] = scraper
        logger.debug("Registered built-in scraper: %s", scraper.name)

    @property
    def builtin_scrapers(self):
        return dict(self._builtins)

    def get_builtin(self, name):
        return self._builtins.get(name)

    def all_builtin_names(self):
        return list(self._builtins.keys())

    # ── DB seed ─────────────────────────────────────────────────────────────

    def seed_db(self, app):
        """
        Ensure every built-in scraper has a row in scraper_sources.
        Safe to call multiple times — only inserts missing rows.
        """
        from models import ScraperSource
        from extensions import db

        with app.app_context():
            for name, scraper in self._builtins.items():
                if not ScraperSource.query.filter_by(name=name).first():
                    row = ScraperSource(
                        name=name,
                        display_name=scraper.display_name,
                        description=scraper.description,
                        source_type=scraper.source_type,
                        is_builtin=True,
                        is_enabled=True,
                        search_terms=json.dumps(scraper.default_search_terms),
                    )
                    db.session.add(row)
            db.session.commit()
            logger.info("Scraper DB rows seeded.")

    # ── Fetch all enabled ────────────────────────────────────────────────────

    def fetch_all_enabled(self, app, on_source=None):
        """
        Run every enabled scraper (built-in + custom) and return:
          (all_jobs: list[dict], statuses: dict[name -> "" | error_message])
        Updates last_run / last_status on each ScraperSource row.

        on_source (optional callable):
          Called as on_source(event, display_name, url, count) where event is
          "start" (before fetch), "success", "error", or "skipped".
        """
        from models import ScraperSource
        from extensions import db

        results = []
        statuses = {}

        with app.app_context():
            sources = ScraperSource.query.filter_by(is_enabled=True).all()

            for row in sources:
                url = row.url_template or ""
                scraper = self._resolve(row)
                if scraper is None:
                    if on_source:
                        on_source("skipped", row.display_name, url, 0)
                    continue

                if on_source:
                    on_source("start", row.display_name, url, 0)

                terms = row.get_search_terms() or None
                try:
                    jobs = scraper.fetch(search_terms=terms)
                    results.extend(jobs)
                    statuses[row.name] = ""
                    row.last_run = datetime.utcnow()
                    row.last_status = "success"
                    row.last_jobs_found = len(jobs)
                    row.last_error = None
                    if on_source:
                        on_source("success", row.display_name, url, len(jobs))
                except Exception as exc:
                    msg = str(exc)
                    logger.error("Scraper '%s' error: %s", row.name, msg)
                    statuses[row.name] = msg
                    row.last_run = datetime.utcnow()
                    row.last_status = "error"
                    row.last_error = msg
                    if on_source:
                        on_source("error", row.display_name, url, 0)

            db.session.commit()

        logger.info(
            "fetch_all_enabled: %d jobs from %d enabled sources",
            len(results), len(sources),
        )
        return results, statuses

    def _resolve(self, row):
        """Map a ScraperSource DB row to a live JobScraper instance."""
        if row.source_type == "builtin":
            return self._builtins.get(row.name)
        if row.source_type == "rss":
            return RSSFeedScraper(
                name=row.name,
                display_name=row.display_name,
                url_template=row.url_template or "",
                description=row.description or "",
            )
        if row.source_type == "json_api":
            return JSONAPIScraper(
                name=row.name,
                display_name=row.display_name,
                url_template=row.url_template or "",
                description=row.description or "",
            )
        logger.warning("Unknown source_type '%s' for '%s'", row.source_type, row.name)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Module-level registry — import and use this everywhere
# ══════════════════════════════════════════════════════════════════════════════

registry = ScraperRegistry()

# ── Register built-in scrapers (add new ones here) ───────────────────────────
registry.register_builtin(RemoteOKScraper())
registry.register_builtin(RemotiveScraper())
registry.register_builtin(WWRScraper())
registry.register_builtin(LinkedInScraper())
registry.register_builtin(USAJobsScraper())        # requires free API key — see class docstring
registry.register_builtin(CareerOneStopScraper())  # requires free API key — see class docstring
# registry.register_builtin(MyNewScraper())   ← add your own here


# ══════════════════════════════════════════════════════════════════════════════
# Unavailable sources — displayed on the Sources page as informational cards
# These sources CANNOT be legally or technically scraped at this time.
# ══════════════════════════════════════════════════════════════════════════════

UNAVAILABLE_SOURCES = [
    {
        "name": "indeed",
        "display_name": "Indeed",
        "icon": "bi-search-heart",
        "status": "unavailable",
        "status_label": "No Legal Access",
        "status_color": "danger",
        "reason": "RSS Discontinued + No Free Public API + ToS Prohibits Scraping",
        "detail": (
            "Indeed's public RSS feeds were discontinued in March 2026. "
            "Indeed has no free public API — their official API requires a paid "
            "employer/publisher partnership. Their Terms of Service explicitly prohibit "
            "automated scraping of job listings. Reverse-engineering their internal API "
            "(e.g. via python-jobspy) violates those terms and is not included in this app."
        ),
        "future": (
            "If Indeed ever launches a free public API for job seekers, it will be added "
            "here. For now, use USAJobs or CareerOneStop for free legal job listings."
        ),
        "url": "https://www.indeed.com",
    },
    {
        "name": "ziprecruiter",
        "display_name": "ZipRecruiter",
        "icon": "bi-briefcase-x",
        "status": "unavailable",
        "status_label": "Unavailable",
        "status_color": "danger",
        "reason": "RSS API Deprecated + Cloudflare Bot Protection",
        "detail": (
            "ZipRecruiter's ZipSearch RSS API was officially shut down on March 31, 2025. "
            "The HTML search results page is now protected by Cloudflare Bot Management "
            "(enterprise tier), which blocks all automated access including common scraping "
            "libraries (returns 403 Forbidden). There is no reliable free method to fetch "
            "ZipRecruiter listings at this time."
        ),
        "future": (
            "May become available if ZipRecruiter launches a new public API or partner program. "
            "Check ziprecruiter.com/employers/products/apply-connect for updates."
        ),
        "url": "https://www.ziprecruiter.com",
    },
    {
        "name": "onet",
        "display_name": "O*NET Online",
        "icon": "bi-diagram-3",
        "status": "not_a_job_board",
        "status_label": "Skills Database",
        "status_color": "info",
        "reason": "Occupational Data API — Not a Job Listings Site",
        "detail": (
            "O*NET (Occupational Information Network) is a free U.S. Department of Labor "
            "database of occupational data, not a job listings site. It provides detailed "
            "profiles of 900+ occupations: required skills, typical tasks, knowledge areas, "
            "education requirements, and salary ranges. Its free REST API "
            "(services.onetcenter.org, CC BY 4.0 license) returns occupational data — "
            "not job postings."
        ),
        "future": (
            "Planned future integration for skill matching: compare the skills extracted "
            "from your resume against O*NET occupation profiles to automatically score "
            "how well each job listing fits your background. Register free at "
            "services.onetcenter.org."
        ),
        "url": "https://www.onetonline.org",
        "api_url": "https://services.onetcenter.org",
    },
    {
        "name": "jobright",
        "display_name": "JobRight AI",
        "icon": "bi-robot",
        "status": "unavailable",
        "status_label": "Blocked",
        "status_color": "danger",
        "reason": "No Public API + robots.txt Explicitly Blocks All Crawlers",
        "detail": (
            "JobRight AI is an AI-powered job search copilot that aggregates roughly "
            "400,000 jobs daily from across the web. However, it is a closed system: "
            "there is no public API, no RSS feed, and no developer program. Their "
            "robots.txt explicitly blocks all automated crawlers from /jobs/ paths, "
            "including named blocks for ClaudeBot, GPTBot, and other AI agents. "
            "Their Terms of Service page returns a 404, but the robots.txt "
            "restriction alone makes any automated access a clear violation. "
            "There is no legal or technical way to extract job listings from JobRight AI."
        ),
        "future": (
            "No developer program or API partnership currently exists. "
            "Contact jobright.ai directly if you require programmatic data access."
        ),
        "url": "https://jobright.ai",
    },
]


# ── Legacy shim — keeps old import working ───────────────────────────────────
def fetch_all_jobs(sources=None):
    """Deprecated — use registry.fetch_all_enabled(app) instead."""
    all_jobs = []
    names = sources or registry.all_builtin_names()
    for name in names:
        s = registry.get_builtin(name)
        if s:
            try:
                all_jobs.extend(s.fetch())
            except Exception as exc:
                logger.error("Scraper %s crashed: %s", name, exc)
    return all_jobs
