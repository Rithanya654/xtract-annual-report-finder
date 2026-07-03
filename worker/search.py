import httpx
import re
import os
import json
import time
import asyncio
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from ddgs import DDGS
from openai import OpenAI

from worker.pdf_validator import (
    is_junk, is_high_risk_domain, score_pdf, year_valid,
    pdf_has_required_statements, pdf_has_required_statements_strict,
    pdf_has_minimum_content, extract_pdf_text,
    pdf_matches_company_tokens, has_current_year, best_year_in,
    _accept_years, domain_matches_entity,
)
from shared.models import CompanyRow

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

PAGE_FETCH_TIMEOUT = 12
PDF_DOWNLOAD_TIMEOUT = 30

# Third-party data portals to search when primary search fails
# ── SSRF protection ───────────────────────────────────────────────────────────

_PRIVATE_HOST_RE = re.compile(
    r'^('
    r'localhost'
    r'|127(?:\.\d+){3}'
    r'|0\.0\.0\.0'
    r'|10(?:\.\d+){3}'
    r'|172\.(?:1[6-9]|2\d|3[01])(?:\.\d+){2}'
    r'|192\.168(?:\.\d+){2}'
    r'|169\.254(?:\.\d+){2}'          # link-local / AWS / GCP metadata
    r'|metadata\.google\.internal'
    r'|::1'
    r'|fd[0-9a-f]{2}:'               # IPv6 ULA
    r')$',
    re.IGNORECASE,
)


def _is_safe_url(url: str) -> bool:
    """Return False for private/internal/localhost targets (SSRF guard)."""
    try:
        host = urlparse(url).hostname or ""
        if not host:
            return False
        return not bool(_PRIVATE_HOST_RE.match(host))
    except Exception:
        return False


# Third-party data portals to search when primary search fails
THIRD_PARTY_DOMAINS = [
    "unternehmensregister.de",
    "mops.twse.com.tw",
    "infogreffe.fr",
    "mas.gov.sg",
    "consult.cbso.nbb.be",
    "lloyds.com",
    "app.companiesoffice.govt.nz",
    "idx.co.id",
    "newsweb.oslobors.no",
    "bseindia.com",
    "latinexbolsa.com",
    "austrian-registers.com",
    "bcb.gov.br",
    "interactive.web.insurance.ca.gov",
]

IR_PATHS = [
    "/investor-relations/annual-reports", "/investor-relations",
    "/ir/annual-report", "/ir/annual-reports", "/ir",
    "/annual-report", "/annual-reports", "/financials",
    "/financial-reports", "/financial-statements", "/reports",
    "/en/investor-relations", "/en/ir", "/en/annual-report",
    "/investors", "/investors/annual-reports",
    "/about/investor-relations", "/corporate/investor-relations",
]


_MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB hard cap on PDF downloads


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_SECONDS_PER_COMPANY = _env_int("XTRACT_MAX_SECONDS_PER_COMPANY", 120)
EXTENDED_SEARCH_SECONDS = _env_int("XTRACT_EXTENDED_SEARCH_SECONDS", 0)
GEMINI_SEARCH_ENABLED = os.environ.get("GEMINI_SEARCH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
GEMINI_SEARCH_MODEL = os.environ.get("GEMINI_SEARCH_MODEL", "gemini-3.5-flash").strip()
GEMINI_SEARCH_SECONDS = _env_int("GEMINI_SEARCH_SECONDS", 90)
OPENAI_LANDING_MODEL = os.environ.get("OPENAI_LANDING_MODEL", "gpt-4o-mini").strip()
OPENAI_VERIFY_MODEL = os.environ.get("OPENAI_VERIFY_MODEL", "gpt-4o-mini").strip()
DDG_ERROR_THRESHOLD = 2

_IR_PAGE_HINT_RE = re.compile(
    r"(investor|annual|financial|report|statement|results|governance|disclosure|publication)",
    re.IGNORECASE,
)


def _row_accept_years(row: CompanyRow) -> frozenset:
    """Use the same acceptance window as local validation testing."""
    return _accept_years(row.expected_year)


def _query_year(accept_years: frozenset | None) -> int | None:
    """Use the middle accepted year when available; otherwise the newest year."""
    if not accept_years:
        return None
    years = sorted(accept_years)
    return years[len(years) // 2]


def _report_search_terms(period_type: str) -> list[str]:
    """
    Return query phrases ordered from specific to generic.
    The current pipeline is still optimized for annual/full-year reports, so
    non-annual period types are treated as hints rather than changing validation.
    """
    raw = (period_type or "").strip()
    lower = raw.lower()

    if not raw or lower in {"year end", "year-end", "annual", "annual report", "fy", "full year", "full-year"}:
        return ["annual report", "financial statements", "year end"]

    normalized = " ".join(raw.split())
    terms = [normalized]
    if "report" not in lower and "statement" not in lower:
        terms.append(f"{normalized} report")
    terms.extend(["financial statements", "annual report"])
    return terms


async def http_get(url: str, timeout: int = PAGE_FETCH_TIMEOUT):
    if not _is_safe_url(url):
        print(f"[SSRF-BLOCK] Rejected private/internal URL: {url[:80]}")
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=HTTP_HEADERS,
            timeout=httpx.Timeout(timeout, write=60),
            event_hooks={"response": [_ssrf_redirect_hook]},
        ) as client:
            r = await client.get(url)
            return r if r.status_code == 200 else None
    except _SSRFRedirectBlocked:
        return None
    except Exception:
        return None


class _SSRFRedirectBlocked(Exception):
    pass


async def _ssrf_redirect_hook(response):
    """httpx event hook: abort if any redirect lands on a private host.
    Relative redirects (no hostname) are fine — httpx resolves them on the
    same origin, so they're not an SSRF risk."""
    if response.is_redirect:
        location = response.headers.get("location", "")
        # Only check absolute URLs — relative paths have no hostname to block
        if location and location.startswith("http") and not _is_safe_url(location):
            print(f"[SSRF-BLOCK] Redirect to private host: {location[:80]}")
            raise _SSRFRedirectBlocked()


async def download_pdf(url: str) -> bytes | None:
    """
    Download and validate a PDF.
    Accepts by Content-Type OR by magic bytes (%PDF) — handles misconfigured servers.
    Rejects files over 50 MB.
    """
    if not _is_safe_url(url):
        print(f"[SSRF-BLOCK] PDF download blocked for private URL: {url[:80]}")
        return None
    r = await http_get(url, timeout=PDF_DOWNLOAD_TIMEOUT)
    if r is None:
        return None
    ct = r.headers.get("content-type", "").lower()
    content = bytes(r.content)
    if len(content) > _MAX_PDF_BYTES:
        print(f"[REJECT-TOO-LARGE] {len(content) // 1024 // 1024}MB PDF: {url[:80]}")
        return None
    if "pdf" in ct or content[:4] == b"%PDF":
        return content
    return None


async def fetch_html(url: str) -> str:
    r = await http_get(url)
    if r is None:
        return ""
    ct = r.headers.get("content-type", "").lower()
    text = r.text or ""
    if "html" in ct or "<html" in text[:500].lower():
        return text
    return ""


def _normalize_domain_url(domain: str) -> str:
    raw = (domain or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


def _same_host(url_a: str, url_b: str) -> bool:
    try:
        return (urlparse(url_a).hostname or "").lower() == (urlparse(url_b).hostname or "").lower()
    except Exception:
        return False


def extract_candidate_page_links(html: str, base_url: str, limit: int = 8) -> list[str]:
    """
    Extract same-host HTML pages that look like IR/financial pages.
    Used as a lightweight fallback when a landing page has no direct PDFs.
    """
    a_re = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    seen: set[str] = set()
    candidates: list[tuple[int, str]] = []

    for m in a_re.finditer(html):
        href, anchor = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2)).strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        page_url = urljoin(base_url, href)
        if page_url in seen or not _is_safe_url(page_url):
            continue
        seen.add(page_url)

        parsed = urlparse(page_url)
        if not _same_host(base_url, page_url):
            continue
        if re.search(r"\.pdf(\?|$)", page_url, re.IGNORECASE):
            continue
        if parsed.fragment:
            page_url = page_url.split("#", 1)[0]
        if parsed.path and os.path.splitext(parsed.path)[1].lower() in {".jpg", ".jpeg", ".png", ".zip", ".doc", ".docx", ".xls", ".xlsx"}:
            continue

        haystack = f"{page_url} {anchor}"
        if not _IR_PAGE_HINT_RE.search(haystack):
            continue

        score = 0
        lower = haystack.lower()
        for token in ("annual", "report", "financial", "statement", "investor", "results"):
            if token in lower:
                score += 1
        candidates.append((score, page_url))

    candidates.sort(key=lambda x: x[0], reverse=True)
    ordered: list[str] = []
    for _, page_url in candidates:
        if page_url not in ordered:
            ordered.append(page_url)
        if len(ordered) >= limit:
            break
    return ordered


async def _search_html_page(
    page_url: str,
    source_label: str,
    agent: str,
    country: str,
    tried_urls: set,
    accept_years: frozenset,
    deadline: float,
    pdf_limit: int = 10,
    page_limit: int = 4,
    domain: str = "",
) -> tuple[bytes | None, str, str]:
    """
    Scrape one HTML page for direct PDF links, then follow a few relevant same-host
    IR/financial subpages if needed.
    """
    if time.monotonic() >= deadline or page_url in tried_urls:
        return None, "", ""

    tried_urls.add(page_url)
    html = await fetch_html(page_url)
    if not html:
        return None, "", ""

    last_reject_reason = ""
    for _, pdf_url, anchor, ctx in extract_pdf_links(html, page_url)[:pdf_limit]:
        if time.monotonic() >= deadline:
            break
        pdf_data, found_url, reject_reason = await _try_pdf(
            pdf_url, anchor, ctx, agent, country, tried_urls,
            accept_years=accept_years, domain=domain,
        )
        last_reject_reason = reject_reason or last_reject_reason
        if pdf_data:
            print(f"[{agent}] Found via {source_label}: {found_url}")
            return pdf_data, found_url, ""

    for candidate_url in extract_candidate_page_links(html, page_url, limit=page_limit):
        if time.monotonic() >= deadline:
            break
        if candidate_url in tried_urls:
            continue
        nested_html = await fetch_html(candidate_url)
        tried_urls.add(candidate_url)
        if not nested_html:
            continue
        for _, pdf_url, anchor, ctx in extract_pdf_links(nested_html, candidate_url)[:5]:
            if time.monotonic() >= deadline:
                break
            pdf_data, found_url, reject_reason = await _try_pdf(
                pdf_url, anchor, ctx, agent, country, tried_urls,
                accept_years=accept_years, domain=domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                print(f"[{agent}] Found via {source_label} subpage: {found_url}")
                return pdf_data, found_url, ""

    return None, "", last_reject_reason


async def probe_official_domain(
    domain_url: str,
    agent: str,
    country: str,
    tried_urls: set,
    deadline: float,
    accept_years: frozenset,
) -> tuple[bytes | None, str, str]:
    """
    Probe the company's own site directly before metasearch fallback.
    Keeps the existing flow, but adds a cheaper and often more reliable official-site pass.
    """
    domain_url = _normalize_domain_url(domain_url)
    if not domain_url or is_junk(domain_url) or is_high_risk_domain(domain_url):
        return None, "", ""

    last_reject_reason = ""
    root_pdf, root_url, reject_reason = await _search_html_page(
        domain_url, "official domain root", agent, country, tried_urls,
        accept_years, deadline, pdf_limit=8, page_limit=6, domain=domain_url,
    )
    last_reject_reason = reject_reason or last_reject_reason
    if root_pdf:
        return root_pdf, root_url, ""

    for path in IR_PATHS:
        if time.monotonic() >= deadline:
            break
        page_url = f"{domain_url}{path}"
        pdf_data, found_url, reject_reason = await _search_html_page(
            page_url, "official domain path", agent, country, tried_urls,
            accept_years, deadline, pdf_limit=6, page_limit=3, domain=domain_url,
        )
        last_reject_reason = reject_reason or last_reject_reason
        if pdf_data:
            return pdf_data, found_url, ""

    return None, "", last_reject_reason


def _is_ddg_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in (
        "startpage.com",
        "search.yahoo.com",
        "connecterror",
        "connecttimeout",
        "request timed out",
        "tls close_notify",
        "peer closed connection",
        "connection refused",
    ))


def extract_pdf_links(html: str, base_url: str) -> list[tuple[int, str, str, str]]:
    """
    Extract all PDF-candidate links from HTML.
    Returns list of (score, url, anchor_text, context) sorted: 2026 first → score → year.
    """
    a_re   = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    raw_re = re.compile(r'(https?://[^\s"\'<>]+\.pdf(?:\?[^\s"\'<>]*)?)', re.IGNORECASE)
    seen: set[str] = set()
    scored: list[tuple[int, str, str, str]] = []

    def add(raw_url: str, anchor: str = "", ctx: str = "") -> None:
        raw_url = raw_url.strip().split("#")[0]
        if not raw_url:
            return
        if not raw_url.startswith("http"):
            raw_url = urljoin(base_url, raw_url)
        if raw_url in seen:
            return
        seen.add(raw_url)
        anc = re.sub(r"<[^>]+>", " ", anchor).strip()
        c   = re.sub(r"<[^>]+>", " ", ctx).strip()
        scored.append((score_pdf(raw_url, anc, c), raw_url, anc, c))

    for m in a_re.finditer(html):
        href, anch = m.group(1), m.group(2)
        ext = os.path.splitext(urlparse(href).path)[1].lower()
        start, end = max(0, m.start() - 300), min(len(html), m.end() + 300)
        ctx = html[start:end]
        if ext == ".pdf" or "pdf" in href.lower() or "annual" in (href + anch).lower():
            add(href, anch, ctx)

    for m in raw_re.finditer(html):
        start, end = max(0, m.start() - 200), min(len(html), m.end() + 200)
        add(m.group(1), "", html[start:end])

    scored.sort(key=lambda x: (
        1 if has_current_year(f"{x[1]} {x[2]} {x[3]}") else 0,
        x[0],
        best_year_in(f"{x[1]} {x[2]} {x[3]}")
    ), reverse=True)
    return scored


# ── GPT functions (sync OpenAI client called from async context) ──────────────

def _openai_client() -> OpenAI | None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return OpenAI(api_key=key) if key else None


def _gemini_client():
    key = os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=key)
    except Exception as e:
        print(f"[GEMINI-INIT-ERR] {e}")
        return None


def _extract_urls_from_model_text(raw: str) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    candidates: list[str] = []
    json_texts = [text]
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        json_texts.insert(0, match.group(0))

    for item in json_texts:
        try:
            parsed = json.loads(item)
        except Exception:
            continue
        if isinstance(parsed, list):
            candidates.extend(str(u).strip() for u in parsed)
            break

    candidates.extend(re.findall(r"https?://[^\s\"'<>),\]]+", text))

    urls: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        url = url.strip().rstrip(".,;")
        if not url.startswith("http") or url in seen or not _is_safe_url(url):
            continue
        seen.add(url)
        urls.append(url)
    return urls[:8]


async def gemini_find_candidate_urls(
    agent: str, country: str, domain: str = "", accept_years: frozenset | None = None,
    period_type: str = "Annual Report",
) -> list[str]:
    """Use Gemini Google Search grounding as an optional final URL discovery fallback."""
    if not GEMINI_SEARCH_ENABLED:
        return []

    client = _gemini_client()
    if not client:
        return []

    if accept_years is None:
        accept_years = _accept_years()
    years = " or ".join(str(y) for y in sorted(accept_years))
    domain_hint = f"\nKnown official domain, if useful: {domain}" if domain else ""
    prompt = (
        f"Find official annual report or financial statement PDF URLs/pages for this company.\n"
        f"Company: {agent}\n"
        f"Country: {country or 'unknown'}\n"
        f"Report type: {period_type or 'Annual Report'}\n"
        f"Accepted reporting year: {years}\n"
        f"Important: prefer official company/investor-relations pages and direct PDF URLs. "
        f"The final PDF must contain a Balance Sheet / Statement of Financial Position.{domain_hint}\n"
        f"Return ONLY a JSON array of up to 8 URL strings. No markdown, no explanation."
    )

    def _call() -> str:
        try:
            interaction = client.interactions.create(
                model=GEMINI_SEARCH_MODEL,
                input=prompt,
                tools=[{"type": "google_search"}],
            )
            return getattr(interaction, "output_text", "") or str(interaction)
        except Exception as interactions_error:
            try:
                from google.genai import types
                response = client.models.generate_content(
                    model=GEMINI_SEARCH_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    ),
                )
                return getattr(response, "text", "") or str(response)
            except Exception as generate_error:
                print(f"[GEMINI-SEARCH-ERR] interactions={interactions_error} generate={generate_error}")
                return ""

    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, _call)
        urls = _extract_urls_from_model_text(raw)
        print(f"[GEMINI-SEARCH] {agent} | urls={urls}")
        return urls
    except Exception as e:
        print(f"[GEMINI-SEARCH-ERR] {agent} | {e}")
        return []


async def gpt_find_landing_page(
    agent: str, country: str, accept_years: frozenset | None = None
) -> list[str]:
    """Ask GPT-4o-mini for IR landing page URLs. Returns [] when no key or on error."""
    client = _openai_client()
    if not client:
        return []
    try:
        prompt = (
            f"Company: {agent}\nCountry code: {country}\n\n"
            f"Give me up to 5 URLs where the "
            f"{_query_year(accept_years) if accept_years else 'latest'} annual report "
            f"or financial statements can be found for this company. Prefer the "
            f"company's own annual reports page or a direct PDF link. "
            f"Do NOT return third-party aggregator sites. "
            f"Return ONLY a JSON array of URL strings, nothing else.\n"
            f'Example: ["https://www.company.com/investor-relations/annual-reports"]'
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: client.chat.completions.create(
            model=OPENAI_LANDING_MODEL,
            max_tokens=300,
            temperature=0,
            messages=[
                {"role": "system", "content": "You are a financial data researcher. Return only valid JSON arrays of URLs."},
                {"role": "user", "content": prompt},
            ]
        ))
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        urls = json.loads(raw)
        if isinstance(urls, list):
            valid = [u for u in urls if isinstance(u, str) and u.startswith("http")]
            print(f"[GPT-LANDING] {agent} | {valid}")
            return valid[:5]
    except Exception as e:
        print(f"[GPT-LANDING-ERR] {agent} | {e}")
    return []


async def gpt_verify_pdf(pdf_text: str, agent: str, country: str,
                         accept_years: frozenset | None = None) -> tuple[bool | None, str]:
    """
    Verify PDF against company, year, Balance Sheet and report-type signals via GPT-4o-mini.
    Returns:
      (True,  reason) — all 3 YES
      (False, reason) — any NO
      (None,  reason) — API error / no key → caller runs local fallback
    """
    client = _openai_client()
    if not client:
        return None, "No OpenAI API key — using local checks"

    if not pdf_text.strip():
        return False, "PDF unreadable"

    if accept_years is None:
        accept_years = _accept_years()

    try:
        snippet = (pdf_text[500:5000] + "\n...\n" + pdf_text[-1500:]).strip()
        prompt = (
            f"Company we are looking for: {agent} (country: {country})\n\n"
            f"PDF text excerpt:\n{snippet}\n\n"
            f"Answer these 4 questions with YES or NO each, on separate lines:\n"
            f"1. Does this PDF belong to {agent} or its direct subsidiary?\n"
            f"2. Is the reporting/fiscal year {' or '.join(str(y) for y in sorted(accept_years))} "
            f"(NOT {min(accept_years) - 1} or earlier)?\n"
            f"3. Does this PDF contain a Balance Sheet / Statement of Financial Position "
            f"with figures — not just mentions?\n"
            f"4. Is this a complete financial/annual report (NOT a press release, "
            f"investor summary, presentation, brochure, or notice)?"
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: client.chat.completions.create(
            model=OPENAI_VERIFY_MODEL,
            max_tokens=60,
            temperature=0,
            messages=[
                {"role": "system", "content": "Answer each question with only YES or NO on a separate line. No other text."},
                {"role": "user", "content": prompt},
            ]
        ))
        answers = [a.strip() for a in resp.choices[0].message.content.strip().upper().splitlines() if a.strip()]
        print(f"[GPT-VERIFY] {agent} | answers={answers}")

        # Lenient policy (bias toward keeping real reports):
        #   • Question 1 (company identity) is the ONLY hard gate. If GPT is sure
        #     it's the wrong company → reject (False).
        #   • If company matches AND year matches → accept provisionally; caller
        #     still requires the local Balance Sheet check.
        #   • Any other doubt (year unsure, tables/completeness "NO", or an
        #     incomplete response) → return None so local checks get a second
        #     chance instead of throwing a genuine report away.
        if not answers:
            return None, "GPT empty response — using local checks"

        company_ok = answers[0].startswith("Y")
        if not company_ok:
            return False, f"GPT: wrong company ({answers[0]})"

        year_ok = len(answers) > 1 and answers[1].startswith("Y")
        if year_ok:
            return True, "GPT verified: right company + right year ✓"

        # Right company but GPT unsure about year/tables/completeness → soft pass
        return None, f"GPT: company OK but unsure (answers={answers}) — using local checks"

    except Exception as e:
        print(f"[GPT-VERIFY-ERR] {agent} | {e}")
        return None, f"GPT error: {e}"


async def _local_verify(pdf_bytes: bytes, pdf_text: str, url: str,
                        anchor: str, ctx: str, agent: str,
                        accept_years: frozenset | None = None,
                        gpt_company_confirmed: bool = False) -> tuple[bool, str]:
    """
    Local fallback verification when GPT is unavailable or couldn't confirm
    all criteria.  When gpt_company_confirmed=True, GPT already said this is
    the right company so we skip the token name-match check.
    """
    if not pdf_text.strip():
        return False, "unreadable PDF"

    if not pdf_has_minimum_content(pdf_text):
        return False, f"local: PDF too short ({len(pdf_text.split())} words)"

    if not year_valid(pdf_bytes, url, anchor, ctx, accept=accept_years):
        return False, "local: wrong year"

    ok, stmt_reason = pdf_has_required_statements(pdf_bytes)
    if not ok:
        return False, f"local: {stmt_reason}"

    if not gpt_company_confirmed and not pdf_matches_company_tokens(pdf_text, agent):
        return False, f"local: company name mismatch for '{agent}'"

    company_note = "GPT confirmed company" if gpt_company_confirmed else "company name ✓"
    return True, f"local checks passed (year ✓ | {stmt_reason} | {company_note})"


async def _try_pdf(url: str, anchor: str, ctx: str, agent: str, country: str,
                   tried_urls: set,
                   accept_years: frozenset | None = None,
                   domain: str = "") -> tuple[bytes | None, str, str]:
    """
    Download + verify one PDF URL.
    Verification order:
      1. GPT verify (company + year) — if True, require local Balance Sheet check
      2. GPT returns False → reject immediately
      3. GPT unavailable/error → full local fallback (year + statements + token match)
    Unreadable PDFs always rejected.
    """
    if url in tried_urls:
        return None, "", ""
    tried_urls.add(url)

    if is_junk(url) or is_high_risk_domain(url):
        return None, "", "rejected: junk or high-risk domain"

    if not re.search(r"\.pdf(\?|$)", url, re.IGNORECASE):
        return None, "", ""

    pdf_data = await download_pdf(url)
    if not pdf_data:
        return None, "", "rejected: could not download PDF"

    if not domain_matches_entity(url, domain, agent):
        reason = f"rejected: URL domain does not match entity/domain ({domain or agent})"
        print(f"[REJECT-DOMAIN-MISMATCH] {url[:80]}")
        return None, "", reason

    pdf_text = extract_pdf_text(pdf_data, max_pages=15)

    # Unreadable PDF — reject immediately, don't even try GPT
    if not pdf_text.strip():
        print(f"[REJECT-UNREADABLE] {url[:80]}")
        return None, "", "rejected: unreadable PDF"

    # Too short to be a real financial report (flyer, notice, 1-pager)
    if not pdf_has_minimum_content(pdf_text):
        words = len(pdf_text.split())
        print(f"[REJECT-TOO-SHORT] {words} words | {url[:80]}")
        return None, "", f"rejected: PDF text too short ({words} words)"

    # NOTE: no hard token pre-gate here on purpose — GPT is better than token
    # matching at recognising subsidiaries, abbreviations and logo-only covers,
    # so we let GPT decide first and only fall back to token matching when GPT
    # is unavailable (inside _local_verify). This keeps real reports from being
    # rejected just because the legal name isn't in the first 15 pages.
    gpt_ok, reason = await gpt_verify_pdf(pdf_text, agent, country, accept_years=accept_years)

    if gpt_ok is True:
        ok, stmt_reason = pdf_has_required_statements(pdf_data)
        if not ok:
            print(f"[BALANCE-SHEET-REJECT] {agent} | {stmt_reason} | {url[:80]}")
            return None, "", f"rejected: {stmt_reason}"
        print(f"[GPT-ACCEPT] {agent} | {stmt_reason} | {url[:80]}")
        return pdf_data, url, ""

    if gpt_ok is False:
        # GPT is confident this is the WRONG COMPANY — trust it, do not recover.
        print(f"[GPT-REJECT] {agent} | {reason} | {url[:80]}")
        return None, "", f"rejected: {reason}"

    # gpt_ok is None → GPT unavailable OR unsure about year/tables/completeness.
    # If GPT said company=YES (answers[0] starts with Y), pass that confirmation
    # through so local checks skip the redundant token name-match.
    _gpt_confirmed_company = False
    if gpt_ok is None:
        try:
            # Peek at GPT answer from reason string — set flag if company was YES
            _gpt_confirmed_company = "company OK" in reason
        except Exception:
            pass
    local_ok, local_reason = await _local_verify(pdf_data, pdf_text, url, anchor, ctx, agent,
                                                  accept_years=accept_years,
                                                  gpt_company_confirmed=_gpt_confirmed_company)
    if local_ok:
        print(f"[LOCAL-ACCEPT] {agent} | {local_reason} | {url[:80]}")
        return pdf_data, url, ""

    print(f"[LOCAL-REJECT] {agent} | {local_reason} | {url[:80]}")
    return None, "", f"rejected: {local_reason}"


async def search_by_name(
    agent: str, country: str = "", domain: str = "",
    tried_urls: set | None = None, deadline: float | None = None,
    accept_years: frozenset | None = None,
    period_type: str = "Year End",
) -> tuple[bytes | None, str, str]:
    """
    Primary search: GPT landing pages → scrape → DDG fallback → third-party portals.
    Returns (pdf_bytes, pdf_url, reject_reason).
    """
    if tried_urls is None:
        tried_urls = set()
    if deadline is None:
        deadline = time.monotonic() + MAX_SECONDS_PER_COMPANY
    if accept_years is None:
        accept_years = _accept_years()
    last_reject_reason = ""

    def _timed_out() -> bool:
        return time.monotonic() >= deadline

    domain_url = _normalize_domain_url(domain)
    ddg_error_count = 0

    # ── Step 1: GPT landing pages ──────────────────────────────────────────
    print(f"[{agent}] Step 1: GPT landing pages (years={sorted(accept_years)})")
    gpt_urls = await gpt_find_landing_page(agent, country, accept_years=accept_years)

    if domain_url:
        if domain_url not in gpt_urls:
            gpt_urls.append(domain_url)

    for land_url in gpt_urls:
        if _timed_out():
            break
        if is_junk(land_url) or is_high_risk_domain(land_url):
            continue
        if re.search(r"\.pdf(\?|$)", land_url, re.IGNORECASE):
            pdf_data, found_url, reject_reason = await _try_pdf(
                land_url, "", "", agent, country, tried_urls,
                accept_years=accept_years, domain=domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                print(f"[{agent}] Found via GPT direct PDF: {found_url}")
                return pdf_data, found_url, ""
        else:
            pdf_data, found_url, reject_reason = await _search_html_page(
                land_url, "GPT landing", agent, country, tried_urls,
                accept_years, deadline, pdf_limit=10, page_limit=4, domain=domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                return pdf_data, found_url, ""

    if not _timed_out() and domain_url:
        print(f"[{agent}] Step 1b: official domain probe")
        pdf_data, found_url, reject_reason = await probe_official_domain(
            domain_url, agent, country, tried_urls, deadline, accept_years
        )
        last_reject_reason = reject_reason or last_reject_reason
        if pdf_data:
            return pdf_data, found_url, ""

    if _timed_out():
        print(f"[{agent}] Time limit ({MAX_SECONDS_PER_COMPANY}s) reached — stopping")
        reason = "rejected: search timed out"
        if last_reject_reason:
            reason = f"{reason}; last rejection: {last_reject_reason}"
        return None, "", reason

    # ── Step 2: DDG fallback — queries targeted to the accepted year(s) ────
    print(f"[{agent}] Step 2: DDG fallback")
    # Use highest accepted year as the primary query year (e.g. current year in phase 1,
    # previous year in phase 2).
    _yr = _query_year(accept_years) or max(accept_years)
    primary_term, secondary_term, fallback_term = _report_search_terms(period_type)[:3]
    ddg_queries = [
        f'"{agent}" "{primary_term}" {_yr} filetype:pdf',
        f'"{agent}" "{secondary_term}" {_yr} filetype:pdf',
        (f'{agent} {fallback_term} {_yr} site:{domain}' if domain
         else f'{agent} {fallback_term} {_yr} pdf'),
        (f'"{agent}" "{country}" "{primary_term}" {_yr}' if country
         else f'"{agent}" "{primary_term}" {_yr}'),
    ]

    for query in ddg_queries:
        if _timed_out():
            break
        try:
            with DDGS() as d:
                results = list(d.text(query, max_results=8))
                for r in results:
                    if _timed_out():
                        break
                    result_url = r.get("href") or r.get("url", "")
                    if not result_url:
                        continue
                    if is_junk(result_url) or is_high_risk_domain(result_url):
                        tried_urls.add(result_url)
                        continue

                    if re.search(r"\.pdf(\?|$)", result_url, re.IGNORECASE):
                        pdf_data, found_url, reject_reason = await _try_pdf(
                            result_url, "", "", agent, country, tried_urls,
                            accept_years=accept_years, domain=domain,
                        )
                        last_reject_reason = reject_reason or last_reject_reason
                        if pdf_data:
                            print(f"[{agent}] Found via DDG direct: {found_url}")
                            return pdf_data, found_url, ""
                    else:
                        tried_urls.add(result_url)
                        html = await fetch_html(result_url)
                        if html:
                            for _, pdf_url, anchor, ctx in extract_pdf_links(html, result_url)[:5]:
                                if _timed_out():
                                    break
                                pdf_data, found_url, reject_reason = await _try_pdf(
                                    pdf_url, anchor, ctx, agent, country, tried_urls,
                                    accept_years=accept_years, domain=domain,
                                )
                                last_reject_reason = reject_reason or last_reject_reason
                                if pdf_data:
                                    print(f"[{agent}] Found via DDG page: {found_url}")
                                    return pdf_data, found_url, ""
        except Exception as e:
            print(f"[{agent}] DDG error: {e}")
            if _is_ddg_transient_error(e):
                ddg_error_count += 1
                if ddg_error_count >= DDG_ERROR_THRESHOLD:
                    print(f"[{agent}] DDG backend unstable after {ddg_error_count} transient failures — skipping remaining DDG queries")
                    break
            continue

    if _timed_out():
        print(f"[{agent}] Time limit ({MAX_SECONDS_PER_COMPANY}s) reached — stopping")
        reason = "rejected: search timed out"
        if last_reject_reason:
            reason = f"{reason}; last rejection: {last_reject_reason}"
        return None, "", reason

    # ── Step 3: Third-party regulatory / exchange portals ─────────────────
    print(f"[{agent}] Step 3: Third-party portals")
    if ddg_error_count >= DDG_ERROR_THRESHOLD:
        print(f"[{agent}] Skipping third-party portal metasearch because DDG backend is unstable for this company")
        return None, "", last_reject_reason or "rejected: search backend unstable"
    pdf_data, found_url, reject_reason = await search_third_party(
        agent, country, tried_urls, deadline, accept_years=accept_years
    )
    last_reject_reason = reject_reason or last_reject_reason
    if pdf_data:
        return pdf_data, found_url, ""

    print(f"[{agent}] Not found")
    return None, "", last_reject_reason or "not found: no acceptable PDF candidate"


async def search_third_party(
    agent: str, country: str, tried_urls: set, deadline: float,
    accept_years: frozenset | None = None,
) -> tuple[bytes | None, str, str]:
    """Step 3: Search known third-party regulatory/exchange portals via DDG site: queries."""
    if accept_years is None:
        accept_years = _accept_years()
    _yr = _query_year(accept_years) or max(accept_years)
    last_reject_reason = ""
    for domain in THIRD_PARTY_DOMAINS:
        if time.monotonic() >= deadline:
            break
        queries = [
            f'{agent} annual report {_yr} site:{domain} filetype:pdf',
            f'{agent} financial statements {_yr} site:{domain}',
            f'{agent} annual report site:{domain}',
        ]
        for query in queries:
            if time.monotonic() >= deadline:
                break
            try:
                with DDGS() as d:
                    results = list(d.text(query, max_results=5))
                    for r in results:
                        if time.monotonic() >= deadline:
                            break
                        result_url = r.get("href") or r.get("url", "")
                        if not result_url:
                            continue
                        if is_junk(result_url) or is_high_risk_domain(result_url):
                            continue

                        if re.search(r"\.pdf(\?|$)", result_url, re.IGNORECASE):
                            pdf_data, found_url, reject_reason = await _try_pdf(
                                result_url, "", "", agent, country, tried_urls,
                                accept_years=accept_years, domain="",
                            )
                            last_reject_reason = reject_reason or last_reject_reason
                            if pdf_data:
                                print(f"[{agent}] Found via third-party ({domain}): {found_url}")
                                return pdf_data, found_url, ""
                        else:
                            if result_url in tried_urls:
                                continue
                            tried_urls.add(result_url)
                            html = await fetch_html(result_url)
                            if html:
                                for _, pdf_url, anchor, ctx in extract_pdf_links(html, result_url)[:5]:
                                    if time.monotonic() >= deadline:
                                        break
                                    pdf_data, found_url, reject_reason = await _try_pdf(
                                        pdf_url, anchor, ctx, agent, country, tried_urls,
                                        accept_years=accept_years, domain="",
                                    )
                                    last_reject_reason = reject_reason or last_reject_reason
                                    if pdf_data:
                                        print(f"[{agent}] Found via third-party page ({domain}): {found_url}")
                                        return pdf_data, found_url, ""
            except Exception as e:
                print(f"[{agent}] Third-party {domain} error: {e}")
                continue

    return None, "", last_reject_reason


async def search_gemini_fallback(
    row: CompanyRow, accept_years: frozenset, deadline: float
) -> tuple[bytes | None, str, str]:
    tried: set[str] = set()
    last_reject_reason = ""
    urls = await gemini_find_candidate_urls(
        row.agent, row.country, row.domain, accept_years=accept_years,
        period_type=row.period_type,
    )

    for url in urls:
        if time.monotonic() >= deadline:
            break
        if is_junk(url) or is_high_risk_domain(url):
            continue

        if re.search(r"\.pdf(\?|$)", url, re.IGNORECASE):
            pdf_data, found_url, reject_reason = await _try_pdf(
                url, "", "", row.agent, row.country, tried,
                accept_years=accept_years, domain=row.domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                print(f"[{row.agent}] Found via Gemini direct PDF: {found_url}")
                return pdf_data, found_url, ""
        else:
            pdf_data, found_url, reject_reason = await _search_html_page(
                url, "Gemini page", row.agent, row.country, tried,
                accept_years, deadline, pdf_limit=10, page_limit=5, domain=row.domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                print(f"[{row.agent}] Found via Gemini page: {found_url}")
                return pdf_data, found_url, ""

    return None, "", last_reject_reason or "not found: Gemini fallback found no acceptable annual report"


async def _search_phase(
    row: CompanyRow, accept_years: frozenset, deadline: float
) -> tuple[bytes | None, str, str]:
    """
    Run one complete search pass (steps 0a → 0b → 1-3) accepting only the specified years.
    Each phase gets a fresh tried_urls set so URLs rejected for wrong year in an earlier
    phase are retried with the relaxed year constraint.
    """
    tried: set[str] = set()
    last_reject_reason = ""

    # Step 0a: direct PDF URL supplied in spreadsheet
    if row.statement_url and re.search(r"\.pdf(\?|$)", row.statement_url, re.IGNORECASE):
        if not is_junk(row.statement_url):
            pdf_data, found_url, reject_reason = await _try_pdf(
                row.statement_url, "", "", row.agent, row.country, tried,
                accept_years=accept_years, domain=row.domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                print(f"[{row.agent}] Found via statement_url: {found_url}")
                return pdf_data, found_url, ""

    # Step 0b: landing page URL from spreadsheet — scrape it for PDFs first
    if row.landing_url and not is_junk(row.landing_url) and not is_high_risk_domain(row.landing_url):
        print(f"[{row.agent}] Step 0: scraping spreadsheet landing URL")
        if re.search(r"\.pdf(\?|$)", row.landing_url, re.IGNORECASE):
            pdf_data, found_url, reject_reason = await _try_pdf(
                row.landing_url, "", "", row.agent, row.country, tried,
                accept_years=accept_years, domain=row.domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                print(f"[{row.agent}] Found via spreadsheet landing PDF: {found_url}")
                return pdf_data, found_url, ""
        else:
            pdf_data, found_url, reject_reason = await _search_html_page(
                row.landing_url, "spreadsheet landing page", row.agent, row.country, tried,
                accept_years, deadline, pdf_limit=10, page_limit=5, domain=row.domain,
            )
            last_reject_reason = reject_reason or last_reject_reason
            if pdf_data:
                return pdf_data, found_url, ""

    # Steps 1-3: full search
    pdf_data, found_url, reject_reason = await search_by_name(
        row.agent, row.country, row.domain, tried, deadline,
        accept_years=accept_years, period_type=row.period_type,
    )
    return pdf_data, found_url, reject_reason or last_reject_reason


async def find_annual_report(row: CompanyRow) -> tuple[bytes | None, str, str]:
    """
    Master resolver.

    If the spreadsheet supplies a year (for example via "Fiscal Year" or
    "Expected Year"), search only that year or those years.

    If no year is supplied, default to the current calendar year only
    (for example on June 24, 2026 this means 2026, not 2025 fallback).
    """
    accept_years = _row_accept_years(row)
    target_year = _query_year(accept_years) or max(accept_years)
    deadline = time.monotonic() + MAX_SECONDS_PER_COMPANY

    print(f"[{row.agent}] === Search: target year {target_year}, accepting {sorted(accept_years)} ===")
    pdf_data, pdf_url, reject_reason = await _search_phase(row, accept_years, deadline)
    if pdf_data:
        return pdf_data, pdf_url, ""

    if EXTENDED_SEARCH_SECONDS and "search timed out" in (reject_reason or "").lower():
        deadline = time.monotonic() + EXTENDED_SEARCH_SECONDS
        print(f"[{row.agent}] Extending timed-out search by {EXTENDED_SEARCH_SECONDS}s")
        pdf_data, pdf_url, extended_reason = await _search_phase(row, accept_years, deadline)
        if pdf_data:
            return pdf_data, pdf_url, ""
        reject_reason = extended_reason or reject_reason

    if GEMINI_SEARCH_ENABLED:
        deadline = time.monotonic() + GEMINI_SEARCH_SECONDS
        print(f"[{row.agent}] Gemini fallback search for up to {GEMINI_SEARCH_SECONDS}s")
        pdf_data, pdf_url, gemini_reason = await search_gemini_fallback(row, accept_years, deadline)
        if pdf_data:
            return pdf_data, pdf_url, ""
        reject_reason = gemini_reason or reject_reason

    print(f"[{row.agent}] Not found")
    return None, "", reject_reason or "not found: no acceptable PDF candidate"
