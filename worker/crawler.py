import re
import asyncio
import httpx
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import tldextract
from ddgs import DDGS

from worker.search import gpt_find_landing_page, HTTP_HEADERS, PAGE_FETCH_TIMEOUT, _is_safe_url
from worker.pdf_validator import extract_years, _accept_years

# ── Constants ──────────────────────────────────────────────────────────────────

LEGAL_SUFFIXES_RE = re.compile(
    r'\b(ltd|limited|inc|incorporated|corp|corporation|co|company|llc|llp|plc|'
    r'ag|sa|sas|sarl|srl|spa|bv|nv|oy|ab|as|asa|gmbh|kk|pte|bhd|berhad|'
    r'holdings|group|bank|insurance|assurance|reinsurance|life|fire|marine|'
    r'general|mutual|pension|fund|the|and|of|for|de|du|la|le|les|'
    r'\(.*?\))\b',
    re.IGNORECASE,
)

LANDING_BLACKLIST = frozenset({
    "simplywall.st", "wisesheets.io", "macrotrends.net", "wsj.com",
    "reuters.com", "bloomberg.com", "ft.com", "forbes.com", "marketwatch.com",
    "investing.com", "finance.yahoo.com", "morningstar.com", "seekingalpha.com",
    "stockanalysis.com", "annualreports.com", "annualreportservice.com",
    "info.creditreform.de", "creditreform.de", "dnb.com", "opencorporates.com",
    "gleif.org", "linkedin.com", "facebook.com", "twitter.com",
    "wikipedia.org", "crunchbase.com", "snowball-analytics.com",
})

# All keywords that signal a link MIGHT contain financial information.
# Intentionally broad — better to over-capture than miss.
_FINANCIAL_URL_KEYWORDS = re.compile(
    r'annual[_\-\s]?report'
    r'|financial[_\-\s]?statement'
    r'|financial[_\-\s]?result'
    r'|financial[_\-\s]?report'
    r'|balance[_\-\s]?sheet'
    r'|income[_\-\s]?statement'
    r'|cash[_\-\s]?flow'
    r'|profit[_\-\s]?loss'
    r'|full[_\-\s]?year'
    r'|year[_\-\s]?end'
    r'|fiscal[_\-\s]?year'
    r'|earnings'
    r'|results'
    r'|disclosure'
    r'|investor[_\-\s]?relation'
    r'|/ir/'
    r'|/ar/'
    r'|annual[_\-\s]?filing'
    r'|form[_\-\s]?20[_\-\s]?f'
    r'|form[_\-\s]?10[_\-\s]?k'
    r'|integrated[_\-\s]?report'
    r'|sustainability[_\-\s]?report'   # some companies bundle this with financials
    r'|interim[_\-\s]?report'
    r'|half[_\-\s]?year'
    r'|quarterly[_\-\s]?report'
    r'|accounts?'
    r'|audited'
    r'|arsredovisning'                 # Swedish
    r'|jahresbericht'                  # German
    r'|rapport[_\-\s]?annuel'         # French
    r'|relatorio[_\-\s]?anual'        # Portuguese
    r'|\bfinancials\b'
    r'|\bstatement\b'
    r'|\bconsolidated\b',
    re.IGNORECASE,
)

# URL types for classification
_TYPE_PATTERNS = [
    ("annual_report",   re.compile(r'annual[_\-\s]?report|arsredovisning|jahresbericht|rapport[_\-\s]?annuel|relatorio[_\-\s]?anual', re.IGNORECASE)),
    ("financial_stmt",  re.compile(r'financial[_\-\s]?statement|balance[_\-\s]?sheet|income[_\-\s]?statement|cash[_\-\s]?flow|profit[_\-\s]?loss|statement\b|accounts?', re.IGNORECASE)),
    ("results",         re.compile(r'earnings|results|full[_\-\s]?year|year[_\-\s]?end|fiscal|quarterly|half[_\-\s]?year|interim', re.IGNORECASE)),
    ("form_filing",     re.compile(r'form[_\-\s]?20[_\-\s]?f|form[_\-\s]?10[_\-\s]?k|annual[_\-\s]?filing', re.IGNORECASE)),
    ("ir_page",         re.compile(r'investor[_\-\s]?relation|/ir/|/ar/|disclosure|audited|consolidated|integrated|financials', re.IGNORECASE)),
    ("pdf",             re.compile(r'\.pdf(\?|$)', re.IGNORECASE)),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def name_tokens(agent: str) -> set[str]:
    cleaned = LEGAL_SUFFIXES_RE.sub(' ', agent.lower())
    return {w for w in cleaned.split() if len(w) >= 4 and w.isalnum()}


def domain_core(url: str) -> str:
    return tldextract.extract(url).domain.lower()


def url_matches_company(url: str, tokens: set[str]) -> bool:
    url_lower = url.lower()
    domain = domain_core(url)
    return any(t in domain or t in url_lower for t in tokens)


async def is_reachable(url: str) -> bool:
    if not _is_safe_url(url):
        return False
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=HTTP_HEADERS) as client:
            r = await client.head(url, timeout=PAGE_FETCH_TIMEOUT)
            if r.status_code < 400:
                return True
            if r.status_code == 405:
                r = await client.get(url, timeout=PAGE_FETCH_TIMEOUT)
                return r.status_code < 400
    except Exception:
        pass
    return False


def validate_url(url: str, tokens: set[str]) -> tuple[bool, str]:
    if not url.startswith('http'):
        return False, "Not HTTP"
    if re.search(r'\.pdf(\?|$)', url, re.IGNORECASE):
        return False, "Direct PDF"
    netloc = urlparse(url.lower()).netloc
    for blocked in LANDING_BLACKLIST:
        if blocked in netloc:
            return False, f"Blacklisted: {blocked}"
    if not url_matches_company(url, tokens):
        return False, "Domain mismatch"
    return True, "OK"


def _url_year_ok(url: str, anchor: str) -> bool:
    """
    Return True if the URL/anchor has no year, or has a year within _accept_years().
    Return False only if it explicitly mentions a year older than last year.
    """
    combined = f"{url} {anchor}"
    years = extract_years(combined)
    if not years:
        return True   # no year → keep (might be current)
    min_ok = min(_accept_years())
    return any(y >= min_ok for y in years)


def _classify_link(url: str, anchor: str) -> str | None:
    """
    Return url_type if the link could contain financial information, else None.
    Checks both URL and anchor text.
    """
    combined = f"{url} {anchor}"

    # If URL is a PDF, only keep it if it has a financial keyword somewhere
    is_pdf = bool(re.search(r'\.pdf(\?|$)', url, re.IGNORECASE))

    matched_type = None
    for type_name, pattern in _TYPE_PATTERNS:
        if pattern.search(combined):
            matched_type = type_name
            break

    if is_pdf:
        # PDF: only keep if there's a financial keyword in url or anchor
        if _FINANCIAL_URL_KEYWORDS.search(combined) or matched_type:
            return matched_type or "pdf"
        return None

    # HTML page: keep if any financial keyword found
    if _FINANCIAL_URL_KEYWORDS.search(combined):
        return matched_type or "ir_page"

    return None


# ── Main functions ────────────────────────────────────────────────────────────

async def find_landing_url(agent: str, country: str) -> tuple[str, str]:
    """
    Find the best IR landing page URL.
    Returns (url, confidence='GPT'|'DDG') or ("", "").
    """
    tokens = name_tokens(agent)

    # Step 1: GPT
    gpt_urls = await gpt_find_landing_page(agent, country)
    for url in gpt_urls:
        # Filter out PDF URLs — we want landing pages only
        if re.search(r'\.pdf(\?|$)', url, re.IGNORECASE):
            continue
        ok, _ = validate_url(url, tokens)
        if ok and await is_reachable(url):
            return url, 'GPT'

    # Step 2: DDG fallback
    queries = [
        f'"{agent}" investor relations annual report',
        f'"{agent}" {country} investor relations' if country else f'"{agent}" IR annual report',
        f'{agent} IR annual report official site',
    ]
    for query in queries:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=10))
                for result in results:
                    url = result.get('href') or result.get('url', '')
                    if not url:
                        continue
                    if re.search(r'\.pdf(\?|$)', url, re.IGNORECASE):
                        continue
                    if not re.search(r'investor|annual|report|/ir/', url, re.IGNORECASE):
                        continue
                    ok, _ = validate_url(url, tokens)
                    if ok and await is_reachable(url):
                        return url, 'DDG'
        except Exception:
            continue

    return "", ""


async def crawl_financial_links(landing_url: str, agent: str) -> list[dict]:
    """
    Fetch landing page and return ALL links that could contain financial information.

    Rules:
    - Capture every link where URL or anchor text mentions any financial keyword
      (annual report, financial statements, results, earnings, IR, 10-K, 20-F, …)
    - PDFs included only when they have a financial keyword in URL or anchor
    - Year filter: skip links that explicitly mention a year < 2025
      (no year in URL = keep — might be current year)
    - Deduplicate by URL
    - 1:many per company — return ALL matches, not just top N
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=HTTP_HEADERS) as client:
            resp = await client.get(landing_url, timeout=PAGE_FETCH_TIMEOUT)
            if resp.status_code >= 400:
                return []
            html = resp.text
    except Exception:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    seen: set[str] = set()
    links: list[dict] = []

    for a_tag in soup.find_all('a', href=True):
        href     = a_tag['href'].strip()
        full_url = urljoin(landing_url, href)
        anchor   = a_tag.get_text(separator=' ', strip=True)

        if full_url in seen:
            continue
        if not full_url.startswith('http'):
            continue
        seen.add(full_url)

        # Year gate: skip if explicitly an old year
        if not _url_year_ok(full_url, anchor):
            continue

        # Classify the link — None means not financial
        url_type = _classify_link(full_url, anchor)
        if url_type is None:
            continue

        links.append({
            'url':      full_url,
            'url_type': url_type,
            'anchor':   anchor[:120],
            'reachable': 'Y',   # default; checked selectively below
        })

    # Also pull any raw PDF URLs embedded in the page source
    for raw_url in re.findall(r'https?://[^\s"\'<>]+\.pdf(?:\?[^\s"\'<>]*)?', html, re.IGNORECASE):
        raw_url = raw_url.split("#")[0]
        if raw_url in seen:
            continue
        if not _url_year_ok(raw_url, ""):
            continue
        if not _FINANCIAL_URL_KEYWORDS.search(raw_url):
            continue
        seen.add(raw_url)
        links.append({
            'url':      raw_url,
            'url_type': 'pdf',
            'anchor':   '',
            'reachable': 'Y',
        })

    if not links:
        return []

    # Check reachability in parallel (cap at 30 concurrent HEAD requests)
    sem = asyncio.Semaphore(30)

    async def check_one(link: dict):
        async with sem:
            try:
                async with httpx.AsyncClient(follow_redirects=True, headers=HTTP_HEADERS) as c:
                    r = await c.head(link['url'], timeout=8)
                    if r.status_code == 405:
                        r = await c.get(link['url'], timeout=8)
                    link['reachable'] = 'Y' if r.status_code < 400 else 'N'
            except Exception:
                link['reachable'] = 'N'

    await asyncio.gather(*[check_one(lk) for lk in links])

    # Keep reachable links; sort: annual_report > financial_stmt > results > form_filing > ir_page > pdf
    _order = {"annual_report": 0, "financial_stmt": 1, "results": 2,
              "form_filing": 3, "ir_page": 4, "pdf": 5}
    links_ok = [lk for lk in links if lk['reachable'] == 'Y']
    links_ok.sort(key=lambda x: _order.get(x['url_type'], 9))

    print(f"[CRAWL] {agent} | landing={landing_url[:60]} | {len(links_ok)} financial links found")
    return links_ok
