import re
import io
import unicodedata
import pdfplumber
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path


def _accept_years(expected_year: int | str | None = None) -> frozenset:
    """
    Return fiscal/reporting years to accept.

    When the spreadsheet provides an expected year, include one year either side
    to handle reports published after fiscal year-end. Otherwise use the current
    year and previous two years.
    """
    y = datetime.now().year
    if expected_year:
        try:
            ey = int(str(expected_year).strip())
            return frozenset({ey - 1, ey, ey + 1})
        except (TypeError, ValueError):
            pass
    return frozenset({y - 2, y - 1, y})


def has_current_year(text: str) -> bool:
    """True if the current calendar year appears in the text."""
    return bool(re.search(rf'\b{datetime.now().year}\b', str(text or "")))


# ── Year-independent scoring patterns ────────────────────────────────────────

_BASE_POSITIVE_PATTERNS = [
    (30, r'\bannual[\s_\-]?report\b'),
    (25, r'\bfinancial[\s_\-]?report\b'),
    (25, r'\bfinancial[\s_\-]?statements?\b'),
    (20, r'\bfull[\s_\-]?year\b'),
    (20, r'\byear[\s_\-]?end\b'),
    (15, r'\bintegrated[\s_\-]?report\b'),
    (15, r'\bform[\s_\-]?20[\s_\-]?f\b'),
    (15, r'\bform[\s_\-]?10[\s_\-]?k\b'),
    (12, r'\bconsolidated\b'),
    (10, r'\bfinancials\b'),
    (10, r'\baudited\b'),
    ( 8, r'\bshareholder[\s_\-]?report\b'),
    ( 5, r'\baccounts?\b'),
    ( 4, r'\bfinance\b'),
    (30, r'年报'), (30, r'年度报告'), (25, r'财务报告'),
    ( 3, r'\bjahresbericht\b'), ( 3, r'\brapport\b'), ( 3, r'\beeff\b'),
]


def _year_patterns() -> list:
    """Return the year-specific scoring entries for the current + previous year."""
    y = datetime.now().year
    prev = y - 1
    yy = str(y)[2:]       # e.g. "26" for 2026
    pp = str(prev)[2:]    # e.g. "25" for 2025
    return [
        (60, rf'\bannual[\s_\-]?report[\s_\-]?{y}\b'),
        (60, rf'\bfinancial[\s_\-]?statements?[\s_\-]?{y}\b'),
        (50, rf'\bfy[\s_\-]?{y}\b'),
        (50, rf'\bfy\s*{yy}\b'),
        (40, rf'\b{y}\b'),
        (35, rf'\bannual[\s_\-]?report[\s_\-]?{prev}\b'),
        (30, rf'\bfy[\s_\-]?{prev}\b'),
        (30, rf'\bfy\s*{pp}\b'),
    ]


# Backward-compat alias — kept so callers that imported the old name still work
POSITIVE_PATTERNS = _BASE_POSITIVE_PATTERNS

NEGATIVE_PATTERNS = [
    (-40, r'\bquarter(?:ly)?\b'), (-40, r'\bq[1-4][\s_\-]?\b'),
    (-30, r'\bproxy\b'), (-30, r'\bprospectus\b'),
    (-25, r'\binterim\b'), (-25, r'\bhalf[\s_\-]?year\b'), (-25, r'\bsemiannual\b'),
    (-20, r'\bsustainab\b'), (-20, r'\besg\b'), (-20, r'\bcsr\b'),
    (-15, r'\bpresentation\b'), (-15, r'\bfactsheet\b'), (-15, r'\bbrochure\b'),
    (-15, r'\btranscript\b'), (-10, r'\bpolicy\b'),
    ( -8, r'\bnotice\b'), ( -8, r'\bagenda\b'), ( -8, r'\bpillar[\s_\-]?3\b'),
    ( -5, r'\brating\b'), ( -5, r'\bpress[_\-]?release\b'),
]

JUNK_URL_PATTERNS = [
    r'\bfitch[_\-]',
    r'swot',
    r'\bmarket[_\-]?research\b',
    r'\bbac[_\-]?swot',
]

JUNK_FILENAMES = {
    "proxy.pdf","index.pdf","download.pdf","file.pdf","document.pdf","get.pdf",
    "view.pdf","pdf.pdf","attachment.pdf","upload.pdf","form.pdf","application.pdf",
    "brochure.pdf","flyer.pdf","press_release.pdf","press-release.pdf",
    "media-release.pdf","notice.pdf","agenda.pdf","invitation.pdf","newsletter.pdf",
    "factsheet.pdf","fact-sheet.pdf","fact_sheet.pdf","tarifas.pdf","tariffs.pdf",
}

JUNK_PATH_PATTERNS = [
    r'/press[_\-]?release', r'/media[_\-]?release', r'/news/', r'/whats[_\-]?new/',
    r'/events?/', r'/tarifas?', r'/tariffs?', r'/csr[_\-]?report', r'/sustainability',
    r'/esg[_\-]', r'/governance', r'/pillar', r'/advisory[_\-]?weekly',
    r'/weekly[_\-]?report', r'/meeting', r'/agm', r'/egm', r'/circular',
]

# All domains from unified_bot.py — never truncate this list
WRONG_COMPANY_DOMAINS = frozenset({
    "www.vtb.com", "www.citigroup.com", "www.deme-group.com",
    "cdn.financialreports.eu", "investor-relations.db.com", "www.apple.com",
    "www.unitedhealthgroup.com", "cdn0.erstegroup.com", "archive.org",
    "www.grupoaval.com", "www.bseindia.com", "www.pipersandler.com",
    "www1.hkexnews.hk", "www.bakermckenzie.com", "eur-lex.europa.eu",
    "www.northerntrust.com", "stocklight.com", "live.euronext.com",
    "cbonds.ru", "www.epfindia.gov.in", "www.sparkassenstiftung.de",
    "financialservices.gov.in", "nsearchives.nseindia.com", "vpr.hkma.gov.hk",
    "report.bvb.de", "ir.po-holdings.co.jp", "cdn.cse.lk",
    "www.ecb.europa.eu", "docs.boursakuwait.com.kw", "www.mhb.de",
    "pei.com.co", "thespargroup.com", "www.suredividend.com",
    "theesk.org", "mb.cision.com", "www.hkexnews.hk", "s25.q4cdn.com",
    "www.arabbank.jo", "global.toyota", "www.alahli.com",
    "www.starlingbank.com", "www.jsafrasarasin.com", "disabilityrightsla.org",
    "www.rbc.com", "on.com.tr", "sbi.bank.in", "www.sc.com",
    "home.treasury.gov", "corporate.asnbank.nl", "companiesmarketcap.com",
    "www.mtn.com", "www.hinghamsavings.com", "www.kbfg.com",
    "invest.bnpparibas", "storage.mfn.se", "www.kuveytturk.com.tr",
    "www.ziraatbank.com.tr", "www.givaudan.com", "eg-bank.com",
    "insights.techmahindra.com", "doclib.ngxgroup.com", "www.rv-re.de",
    "www.johnlewispartnership.co.uk", "www.kotak.bank.in",
    "www.rbinternational.com", "uploads.vw-mms.de",
    "www.annualreports.com", "annualreports.com", "simplywall.st",
    "wisesheets.io", "macrotrends.net", "wsj.com", "reuters.com",
    "bloomberg.com", "ft.com", "forbes.com", "marketwatch.com",
    "investing.com", "finance.yahoo.com", "morningstar.com",
    "seekingalpha.com", "stockanalysis.com", "annualreportservice.com",
    "info.creditreform.de", "creditreform.de", "dnb.com",
    "opencorporates.com", "gleif.org", "linkedin.com", "facebook.com",
    "twitter.com", "wikipedia.org", "crunchbase.com",
    "marketpublishers.com", "www.marketpublishers.com", "pdf.marketpublishers.com",
})


def is_junk(url: str) -> bool:
    lower = url.lower()
    fn = lower.rstrip("/").split("/")[-1].split("?")[0]
    if fn in JUNK_FILENAMES:
        return True
    if any(re.search(p, lower) for p in JUNK_URL_PATTERNS):
        return True
    return any(re.search(p, lower) for p in JUNK_PATH_PATTERNS)


def is_high_risk_domain(url: str) -> bool:
    netloc = urlparse(url.lower()).netloc
    return netloc in WRONG_COMPANY_DOMAINS


def score_pdf(url: str, anchor: str = "", context: str = "") -> int:
    combined = f"{url} {anchor} {context}".lower()
    all_pos = _year_patterns() + _BASE_POSITIVE_PATTERNS
    score = sum(pts for pts, pat in all_pos if re.search(pat, combined, re.IGNORECASE))
    score += sum(pts for pts, pat in NEGATIVE_PATTERNS if re.search(pat, combined, re.IGNORECASE))
    return score


def extract_years(text: str) -> list[int]:
    # Use loose match (no word boundary) so URL patterns like report2025.pdf are caught
    return [2000 + int(m) for m in re.findall(r'20(\d\d)', str(text or ""))]


def best_year_in(text: str) -> int:
    years = extract_years(text)
    return max(years) if years else 0


def year_acceptable(url: str, anchor: str = "", ctx: str = "") -> bool:
    """True if the URL/anchor/ctx has no year (unknown) or has an accepted year."""
    years = extract_years(f"{url} {anchor} {ctx}")
    if not years:
        return True  # unknown — let PDF content decide
    return any(y in _accept_years() for y in years)


def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 25) -> str:
    """Extract text from PDF using pdfplumber. Returns '' on any failure — never raises."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(
                p.extract_text() or "" for p in pdf.pages[:max_pages]
            )
    except Exception:
        return ""


# Patterns that identify the *reporting* year (not a reference year)
_YEAR_PATTERNS = [
    r'(?:year|period|quarter|half.year|interim)\s+ended[^\n]{0,40}(20\d\d)',
    r'annual\s+report[^\n]{0,30}(20\d\d)',
    r'(?:fiscal\s+year|fy\s*)(20\d\d)',
    r'(?:as\s+at|as\s+of)[^\n]{0,30}(20\d\d)',
    r'financial\s+statements[^\n]{0,30}(20\d\d)',
]


def year_valid(pdf_bytes: bytes, url: str, anchor: str = "", ctx: str = "",
               accept: frozenset | None = None) -> bool:
    """
    Check inside the PDF for the reporting year — never trust the URL alone.

    Logic:
    1. Extract text from first 15 pages.
    2. If text found:
       a. Look for explicit reporting-year patterns (year ended / annual report / FY).
          Accept only if year ∈ accept (caller controls which years are valid).
       b. If no explicit pattern → scan for any recent year anywhere in text.
       c. If no year found in readable text → REJECT.
    3. If PDF unreadable → URL year fallback; no year in URL → REJECT.
    """
    if accept is None:
        accept = _accept_years()
    text = extract_pdf_text(pdf_bytes, max_pages=15)

    if text.strip():
        # Step a: explicit reporting-year patterns
        matched_years: set[int] = set()
        for pat in _YEAR_PATTERNS:
            for m in re.findall(pat, text, re.IGNORECASE):
                try:
                    matched_years.add(int(m))
                except ValueError:
                    pass

        if matched_years:
            result = bool(matched_years & accept)
            print(f"[YEAR-EXPLICIT] years={matched_years} accept={result} | {url[:60]}")
            return result

        # Step b: any accepted year anywhere in text
        any_years = {int(y) for y in re.findall(r'\b(20\d\d)\b', text) if int(y) >= min(accept)}
        if any_years:
            result = bool(any_years & accept)
            print(f"[YEAR-ANY] years={any_years} accept={result} | {url[:60]}")
            return result

        # Step c: readable PDF but zero recent years found — REJECT
        print(f"[YEAR-REJECT] Readable PDF but no accepted year found | {url[:60]}")
        return False

    # Unreadable PDF — URL fallback only
    print(f"[YEAR-UNREADABLE] Falling back to URL | {url[:60]}")
    url_years = extract_years(f"{url} {anchor} {ctx}")
    if url_years:
        return bool(set(url_years) & accept)
    print(f"[YEAR-REJECT] Unreadable PDF and no year in URL | {url[:60]}")
    return False


_FS_BALANCE_SHEET = re.compile(
    r'balance\s+sheet'
    r'|statement\s+of\s+financial\s+position'
    r'|financial\s+position'
    r'|assets\s*[&and]+\s*liabilities'
    r'|assets\s+and\s+liabilities',
    re.IGNORECASE,
)

_FS_INCOME = re.compile(
    r'income\s+statement'
    r'|profit\s+(?:and|&)\s+loss'
    r'|(?:p\s*[&/]\s*l\b|p&l)'
    r'|revenue\s+accounts?'
    r'|comprehensive\s+(?:income\s+)?statement'
    r'|statement\s+of\s+(?:comprehensive\s+)?income'
    r'|statement\s+of\s+operations',
    re.IGNORECASE,
)

_FS_CASHFLOW = re.compile(r'cash\s*flow|statement\s+of\s+cash', re.IGNORECASE)
_FS_IFRS17   = re.compile(r'IFRS\s*17', re.IGNORECASE)


MIN_PDF_WORDS = 200


def pdf_has_minimum_content(pdf_text: str) -> bool:
    """Reject blank pages, single-page flyers, and scan-only PDFs with almost no text."""
    return len(pdf_text.split()) >= MIN_PDF_WORDS


def pdf_has_required_statements(pdf_bytes: bytes) -> tuple[bool, str]:
    """
    Scan first 25 pages for mandatory financial statements.

    Rules:
      • Balance Sheet  — MANDATORY. Reject if absent.
      • Income Statement — preferred but not mandatory alone.
      • Cash Flow      — mandatory only when IFRS 17 is detected.
      • Unreadable PDF (scanned/encrypted) — always REJECT.
    """
    text = extract_pdf_text(pdf_bytes, max_pages=25)

    if not text.strip():
        return False, "unreadable PDF — cannot verify contents"

    has_bs  = bool(_FS_BALANCE_SHEET.search(text))
    has_inc = bool(_FS_INCOME.search(text))
    has_cf  = bool(_FS_CASHFLOW.search(text))
    has_i17 = bool(_FS_IFRS17.search(text))

    if not has_bs:
        return False, "no Balance Sheet found"

    if has_i17 and not has_cf:
        return False, "IFRS 17 detected but no Cash Flow statement found"

    parts = ["Balance Sheet ✓"]
    if has_inc: parts.append("Income Statement ✓")
    if has_cf:  parts.append("Cash Flow ✓")
    if has_i17: parts.append("IFRS 17 ✓")
    return True, " | ".join(parts)


def pdf_has_required_statements_strict(pdf_bytes: bytes) -> tuple[bool, str]:
    """
    Stricter version used in local fallback (no GPT available).
    Requires BOTH Balance Sheet AND Income Statement to reduce false positives.
    """
    text = extract_pdf_text(pdf_bytes, max_pages=25)

    if not text.strip():
        return False, "unreadable PDF — cannot verify contents"

    if not pdf_has_minimum_content(text):
        return False, f"PDF too short ({len(text.split())} words) — likely not a full report"

    has_bs  = bool(_FS_BALANCE_SHEET.search(text))
    has_inc = bool(_FS_INCOME.search(text))
    has_cf  = bool(_FS_CASHFLOW.search(text))
    has_i17 = bool(_FS_IFRS17.search(text))

    if not has_bs:
        return False, "no Balance Sheet found"
    if not has_inc:
        return False, "no Income Statement found (strict mode)"
    if has_i17 and not has_cf:
        return False, "IFRS 17 detected but no Cash Flow statement found"

    parts = ["Balance Sheet ✓", "Income Statement ✓"]
    if has_cf:  parts.append("Cash Flow ✓")
    if has_i17: parts.append("IFRS 17 ✓")
    return True, " | ".join(parts)


# ── Token-based company name matching (no API required) ──────────────────────

_NAME_STOPWORDS = {
    "co", "ltd", "limited", "inc", "corp", "corporation", "plc", "ag",
    "sa", "nv", "bv", "ab", "oy", "as", "llc", "llp", "lp", "gmbh",
    "se", "spa", "srl", "sas", "bvba", "cvba", "aps", "pty", "pvt",
    "pte", "bhd", "kk", "bank", "group", "holding", "holdings",
    "financial", "finance", "insurance", "assurance", "trust", "capital",
    "asset", "management", "the", "of", "and", "de", "van",
}


def _strip_diacritics(text: str) -> str:
    """Convert accented/special characters to their ASCII base (ä→a, ö→o, ü→u, etc.)."""
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')


def _normalize_token(token: str) -> str:
    """
    Normalize a token for cross-script matching.
    Handles German-style transliterations (ae→a, oe→o, ue→u) that appear
    in ASCII-ified company names so they match diacritic-stripped PDF text.
    e.g. 'tjaenstepensionsfoerening' → 'tjanstepensionsforening'
    """
    return token.replace('ae', 'a').replace('oe', 'o').replace('ue', 'u')


def name_tokens(agent: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9]{2,}", agent.lower())
    return [t for t in tokens if t not in _NAME_STOPWORDS and len(t) >= 3]


def pdf_matches_company_tokens(pdf_text: str, agent: str) -> bool:
    """
    Token-based company match — no API needed.
    Normalizes both sides to handle diacritics (ä/ö/ü in PDFs vs ASCII
    transliterations in company name inputs like ae/oe/ue).
    Requires at least min(2, len(tokens)) tokens to be found.
    """
    if not pdf_text.strip():
        return False

    # Normalize PDF text: strip diacritics so ä→a, ö→o, ü→u, etc.
    normalized_pdf = _strip_diacritics(pdf_text).lower()

    tokens = name_tokens(agent)
    if not tokens:
        first = re.split(r"[\s,]", agent.strip())[0].lower()
        return len(first) >= 4 and _normalize_token(first) in normalized_pdf

    # Normalize tokens: convert German-style ae/oe/ue → a/o/u to match NFKD output
    found = [t for t in tokens if _normalize_token(t) in normalized_pdf]
    required = min(2, len(tokens))
    result = len(found) >= required
    print(f"[TOKEN-MATCH] {agent} | tokens={tokens[:4]} found={found[:4]} req={required} → {'OK' if result else 'REJECT'}")
    return result


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Return page count of a PDF. Returns 0 on failure."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def domain_matches_entity(pdf_url: str, domain: str, agent: str) -> bool:
    """
    Check if a PDF URL host is plausibly related to the entity.

    If an expected company domain is provided, require a simple root-domain
    match. Without a domain, fall back to entity-name tokens and soft-pass when
    the URL host cannot prove a mismatch.
    """
    if not pdf_url:
        return True

    try:
        pdf_host = urlparse(pdf_url.lower()).netloc
    except Exception:
        return True

    if not pdf_host:
        return True

    if pdf_host.startswith("www."):
        pdf_host = pdf_host[4:]

    if domain:
        raw_domain = domain.lower().strip()
        parsed_domain = urlparse(raw_domain if "://" in raw_domain else f"https://{raw_domain}")
        clean_domain = parsed_domain.netloc or parsed_domain.path
        if clean_domain.startswith("www."):
            clean_domain = clean_domain[4:]
        domain_root = clean_domain.split(".")[0]
        pdf_root = pdf_host.split(".")[0]
        if domain_root and (domain_root in pdf_host or pdf_root in clean_domain):
            return True
        print(f"[DOMAIN-MISMATCH] agent='{agent}' domain='{domain}' pdf_host='{pdf_host}'")
        return False

    tokens = name_tokens(agent)
    if tokens and any(t in pdf_host for t in tokens):
        return True

    return True
