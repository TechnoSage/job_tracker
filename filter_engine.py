"""
filter_engine.py — Scores and categorises job postings based on target skills
"""
import json
import re

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

MICROSOFT_STACK = [
    "C#", "csharp", ".NET", "dotnet", ".NET Core", "NET Core",
    "ASP.NET", "ASP.NET MVC", "ASP.NET Core", "Blazor", "SignalR",
    "Entity Framework", "LINQ", "WPF", "WCF", "MAUI",
    "Azure", "Azure DevOps", "Azure Functions", "Azure SQL",
    "SQL Server", "MSSQL", "T-SQL", "SSRS", "SSIS", "SSAS",
    "Microsoft", "Visual Studio", "Power BI", "PowerShell",
    "Windows Server", "SharePoint", "Teams", "Dynamics 365",
    "NuGet", "MSTest", "xUnit", "NUnit",
]

ORACLE_STACK = [
    "Oracle", "Oracle DB", "Oracle Database", "Oracle RAC",
    "PL/SQL", "PLSQL", "Oracle Forms", "Oracle Reports",
    "Oracle APEX", "Oracle Fusion", "Oracle EBS", "Oracle E-Business",
    "Oracle DBA", "Oracle Cloud", "Oracle Data Guard",
    "Oracle Exadata", "OCI", "Oracle 19c", "Oracle 12c",
]

SOFTWARE_KEYWORDS = [
    "software developer", "software engineer", "programmer", "coder",
    "backend developer", "backend engineer", "full.?stack", "full stack",
    "front.?end", "web developer", "application developer",
    "systems developer", "solutions architect", "tech lead", "architect",
    "devops engineer", "platform engineer", "site reliability",
]

CUSTOMER_SERVICE_KEYWORDS = [
    "customer service", "customer support", "customer success",
    "help desk", "helpdesk", "service desk", "technical support",
    "tier 1", "tier 2", "tier i", "tier ii", "call center",
    "contact center", "support specialist", "support agent",
    "support representative", "client services",
]

IT_KEYWORDS = [
    r"\bIT\b", "information technology", "network engineer", "network admin",
    "system administrator", "sysadmin", "systems admin",
    "infrastructure", "cloud engineer", "database administrator",
    r"\bDBA\b", "devops", "dev ops", "security engineer",
    "cybersecurity", "it specialist", "it support",
]

REMOTE_KEYWORDS = [
    "remote", "work from home", "wfh", "fully remote",
    "distributed", "telecommute", "anywhere",
]

# Job duration detection patterns — ordered most-specific first to avoid misclassification.
# Each tuple: (label, [regex_patterns, ...])
# Searches across title + description + tags (entire posting text).
DURATION_PATTERNS = [
    ("Internship", [
        r"\binternship\b",
        r"\bintern\b(?!\w)",          # "intern" but NOT "internal"
        r"\bco-?op\s+(position|role|student|program)?\b",
        r"\bINTERN\b",                # JSON-LD employmentType value
    ]),
    ("Contract", [
        r"\bcontract[- ]to[- ]hire\b",
        r"\bc2h\b",
        r"\bc2c\b",
        r"\bcorp[- ]to[- ]corp\b",
        r"\b1099\b",
        r"\bindependent\s+contractor\b",
        r"\bcontract\s+(position|role|work|job|basis|assignment|opportunity)\b",
        r"\b(6|12|18|24)[- ]month\s+contract\b",
        r"\bstatement\s+of\s+work\b",  r"\bSOW\b",
        r"\bproject[- ]based\b",
        r"\bCONTRACTOR\b",            # JSON-LD employmentType value
        r"\bon[- ]?demand\b(?!\s+delivery)",
        # "contract" standalone – avoid matching "contract management/negotiation/review"
        r"\bcontract\b(?!\s*(management|admin|administration|negotiation|review|law|officer|specialist))",
    ]),
    ("Part Time", [
        r"part[- ]?time",
        r"\bparttime\b",
        r"\bPART[_\s]TIME\b",         # JSON-LD / API value: PART_TIME or PART TIME
        r"\b\d{1,2}[- ]\d{1,2}\s+hours?\s+(per|a|/)\s*week\b",   # "20-25 hours/week"
        r"\bup\s+to\s+\d{1,2}\s+hours?\s+(per|a)\s+week\b",
        r"\bfewer\s+than\s+40\s+hours?\b",
        r"\bhourly\s+rate\b",
        r"\bper\s+hour\b",
        r"\$[\d,.]+\s*/\s*hr\b",      # "$25/hr"
    ]),
    ("Temporary", [
        r"\btemporary\b",
        r"\btemp[- ]to[- ](perm|permanent|hire)\b",
        r"\btemp\b(?!\s*erature|\s*oral|\s*late|\s*table|\s*est|\s*ing)",
        r"\bshort[- ]term\s+(position|role|contract|assignment|engagement)\b",
        r"\bTEMPORARY\b",             # JSON-LD employmentType value
    ]),
    ("Seasonal", [
        r"\bseasonal\b",
        r"\bholiday\s+(help|hiring|staff|position|work)\b",
    ]),
    ("Freelance", [
        r"\bfreelance\b",
        r"\bfreelancer\b",
        r"\bgig\s+(work|position|role|economy)\b",
        r"\bself[- ]employed\b",
    ]),
    ("Per Diem", [
        r"per[- ]diem",
        r"\bper\s+diem\b",
    ]),
    ("Full Time", [
        r"full[- ]?time",
        r"\bfulltime\b",
        r"\bFULL[_\s]TIME\b",         # JSON-LD / API value: FULL_TIME or FULL TIME
        r"\bdirect\s+hire\b",
        r"\bpermanent\s+(full[- ]?time|position|role|employee|staff|placement)\b",
        r"\b40\+?\s+hours?\s+(per|a|/)\s*week\b",
        r"\bregular[- ]full[- ]?time\b",
        r"\bsalaried\b",              # salaried employee → almost always full-time
        r"\bW-?2\s+employee\b",       # W-2 employee (vs 1099 contractor)
        r"\bFTE\b",                   # Full-Time Equivalent
        r"\bannual\s+(salary|compensation|comp|pay)\b",
        r"\byearly\s+(salary|compensation|comp|pay)\b",
    ]),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matches(text: str, patterns: list) -> list[str]:
    """Return keywords from *patterns* that appear in *text* (case-insensitive)."""
    found = []
    text_lower = text.lower()
    for kw in patterns:
        # Support regex patterns (used for \bIT\b etc.)
        try:
            if re.search(kw, text, re.IGNORECASE):
                found.append(kw.strip(r"\b"))
        except re.error:
            if kw.lower() in text_lower:
                found.append(kw)
    return found


def categorise_job(title: str, description: str) -> str:
    """Return 'software', 'customer_service', 'it', or 'general'."""
    combined = (title + " " + description).lower()
    sw = _matches(combined, SOFTWARE_KEYWORDS)
    cs = _matches(combined, CUSTOMER_SERVICE_KEYWORDS)
    it = _matches(combined, IT_KEYWORDS)
    scores = {"software": len(sw) * 3, "customer_service": len(cs) * 3, "it": len(it) * 3}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def score_job(title: str, description: str, tags: list = None,
              required_skills: list = None, preferred_skills: list = None) -> tuple[int, list]:
    """
    Score a job against target skills.
    Returns (score 0-100, list of matched skill names).
    """
    combined = " ".join([title, description or "", " ".join(tags or [])]).strip()

    matched = []
    score = 0

    # Required skills from config (custom list)
    for skill in (required_skills or []):
        try:
            if re.search(re.escape(skill), combined, re.IGNORECASE):
                matched.append(skill)
                score += 20
        except re.error:
            pass

    # Preferred skills from config
    for skill in (preferred_skills or []):
        try:
            if re.search(re.escape(skill), combined, re.IGNORECASE) and skill not in matched:
                matched.append(skill)
                score += 8
        except re.error:
            pass

    # Microsoft stack bonus
    ms_hits = _matches(combined, MICROSOFT_STACK)
    for hit in ms_hits:
        if hit not in matched:
            matched.append(hit)
            score += 12

    # Oracle stack bonus
    ora_hits = _matches(combined, ORACLE_STACK)
    for hit in ora_hits:
        if hit not in matched:
            matched.append(hit)
            score += 12

    # Remote bonus
    if _matches(combined, REMOTE_KEYWORDS):
        score += 10

    return min(score, 100), list(dict.fromkeys(matched))  # deduplicate, preserve order


def is_relevant(score: int, min_score: int = 25) -> bool:
    return score >= min_score


def detect_job_duration(
    title: str,
    description: str,
    tags: list = None,
    salary_range: str = "",
) -> str:
    """
    Detect job duration/type from title, description, tags, and salary range.
    Searches all available text so nothing is missed.
    Returns one of the DURATION_PATTERNS labels, or empty string if unknown.
    """
    # Strip any residual HTML from description (some RSS feeds return partial HTML)
    desc_text = description or ""
    if "<" in desc_text:
        try:
            from bs4 import BeautifulSoup as _BS
            desc_text = _BS(desc_text, "html.parser").get_text(separator=" ", strip=True)
        except Exception:
            pass

    combined = " ".join([
        title or "",
        desc_text,
        " ".join(tags or []),
        salary_range or "",
    ])

    for label, patterns in DURATION_PATTERNS:
        for pattern in patterns:
            try:
                if re.search(pattern, combined, re.IGNORECASE):
                    return label
            except re.error:
                pass
    return ""


# ---------------------------------------------------------------------------
# US location data
# ---------------------------------------------------------------------------

US_STATES: dict[str, str] = {
    "AL": "Alabama",       "AK": "Alaska",        "AZ": "Arizona",       "AR": "Arkansas",
    "CA": "California",    "CO": "Colorado",      "CT": "Connecticut",   "DE": "Delaware",
    "FL": "Florida",       "GA": "Georgia",       "HI": "Hawaii",        "ID": "Idaho",
    "IL": "Illinois",      "IN": "Indiana",       "IA": "Iowa",          "KS": "Kansas",
    "KY": "Kentucky",      "LA": "Louisiana",     "ME": "Maine",         "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan",      "MN": "Minnesota",     "MS": "Mississippi",
    "MO": "Missouri",      "MT": "Montana",       "NE": "Nebraska",      "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey",    "NM": "New Mexico",    "NY": "New York",
    "NC": "North Carolina","ND": "North Dakota",  "OH": "Ohio",          "OK": "Oklahoma",
    "OR": "Oregon",        "PA": "Pennsylvania",  "RI": "Rhode Island",  "SC": "South Carolina",
    "SD": "South Dakota",  "TN": "Tennessee",     "TX": "Texas",         "UT": "Utah",
    "VT": "Vermont",       "VA": "Virginia",      "WA": "Washington",    "WV": "West Virginia",
    "WI": "Wisconsin",     "WY": "Wyoming",       "DC": "Washington DC",
}

# Direct land neighbors for each state
_STATE_NEIGHBORS: dict[str, list[str]] = {
    "AL": ["FL","GA","TN","MS"],
    "AK": [],
    "AZ": ["CA","NV","UT","CO","NM"],
    "AR": ["MO","TN","MS","LA","TX","OK"],
    "CA": ["OR","NV","AZ"],
    "CO": ["WY","NE","KS","OK","NM","AZ","UT"],
    "CT": ["NY","MA","RI"],
    "DE": ["MD","PA","NJ"],
    "FL": ["GA","AL"],
    "GA": ["FL","AL","TN","NC","SC"],
    "HI": [],
    "ID": ["MT","WY","UT","NV","OR","WA"],
    "IL": ["WI","IA","MO","KY","IN"],
    "IN": ["MI","OH","KY","IL"],
    "IA": ["MN","WI","IL","MO","NE","SD"],
    "KS": ["NE","MO","OK","CO"],
    "KY": ["IL","IN","OH","WV","VA","TN","MO"],
    "LA": ["TX","AR","MS"],
    "ME": ["NH"],
    "MD": ["VA","WV","PA","DE"],
    "MA": ["NY","CT","RI","NH","VT"],
    "MI": ["OH","IN","WI"],
    "MN": ["ND","SD","IA","WI"],
    "MS": ["LA","AR","TN","AL"],
    "MO": ["IA","IL","KY","TN","AR","OK","KS","NE"],
    "MT": ["ND","SD","WY","ID"],
    "NE": ["SD","IA","MO","KS","CO","WY"],
    "NV": ["CA","OR","ID","UT","AZ"],
    "NH": ["ME","VT","MA"],
    "NJ": ["NY","PA","DE"],
    "NM": ["CO","OK","TX","AZ"],
    "NY": ["PA","NJ","CT","MA","VT"],
    "NC": ["VA","TN","GA","SC"],
    "ND": ["MN","SD","MT"],
    "OH": ["PA","WV","KY","IN","MI"],
    "OK": ["KS","MO","AR","TX","NM","CO"],
    "OR": ["WA","ID","NV","CA"],
    "PA": ["NY","NJ","DE","MD","WV","OH"],
    "RI": ["CT","MA"],
    "SC": ["NC","GA"],
    "SD": ["ND","MN","IA","NE","WY","MT"],
    "TN": ["KY","VA","NC","GA","AL","MS","AR","MO"],
    "TX": ["NM","OK","AR","LA"],
    "UT": ["ID","WY","CO","NM","AZ","NV"],
    "VT": ["NY","NH","MA"],
    "VA": ["MD","WV","KY","TN","NC"],
    "WA": ["OR","ID"],
    "WV": ["OH","PA","MD","VA","KY"],
    "WI": ["MN","MI","IL","IA"],
    "WY": ["MT","SD","NE","CO","UT","ID"],
    "DC": ["MD","VA"],
}

_REMOTE_TERMS = ("remote", "work from home", "wfh", "anywhere", "united states", "nationwide", " usa")


def location_passes(job_location: str, applicant_state: str, miles: int) -> bool:
    """
    Return True if the job location is acceptable given the applicant's state and mile radius.

    Rules:
      - applicant_state is empty or contains "remote"  →  always True (no filter)
      - job location contains a remote/national keyword →  always True
      - job location matches applicant's state         →  True
      - miles <= 25: same state only
      - miles <= 75: state + direct border neighbors
      - miles  > 75: state + 2 rings of neighbors
    """
    if not applicant_state or "remote" in applicant_state.lower():
        return True

    loc = (job_location or "").lower()

    # Remote / nationwide jobs always pass
    if any(kw in loc for kw in _REMOTE_TERMS):
        return True

    state_abbr = applicant_state.upper()
    state_full = US_STATES.get(state_abbr, "").lower()

    # Exact state match
    if state_abbr.lower() in loc or (state_full and state_full in loc):
        return True

    if miles <= 25:
        return False

    # Build neighbor set based on miles radius
    neighbors: set[str] = set(_STATE_NEIGHBORS.get(state_abbr, []))
    if miles > 75:
        second_ring: set[str] = set()
        for n in neighbors:
            second_ring.update(_STATE_NEIGHBORS.get(n, []))
        neighbors |= second_ring

    for abbr in neighbors:
        n_full = US_STATES.get(abbr, "").lower()
        if abbr.lower() in loc or (n_full and n_full in loc):
            return True

    return False
