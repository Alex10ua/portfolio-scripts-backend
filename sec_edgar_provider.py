"""
SEC EDGAR XBRL provider — historical data backfill for US-listed tickers.
Free, no API key. SEC requires a descriptive User-Agent identifying the app +
contact (https://www.sec.gov/os/webmaster-faq#developers) — requests without
one get 403'd.
"""
import os
import requests

TICKER_MAP_URL = 'https://www.sec.gov/files/company_tickers.json'
CONCEPT_URL = 'https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json'

USER_AGENT = os.getenv('SEC_USER_AGENT', 'FinancePortfolio admin@financeportfolio.local')
_HEADERS = {'User-Agent': USER_AGENT}

# Reporting concepts that carry share counts, tried in order — companies vary
# which one they tag consistently across filings.
_SHARES_CONCEPTS = [
    ('us-gaap', 'CommonStockSharesOutstanding'),
    ('dei', 'EntityCommonStockSharesOutstanding'),
]

# Curated fundamentals: one internal key -> ordered list of (taxonomy, tag)
# candidates, since filers drift which exact tag they use across years (e.g.
# revenue recognition tags changed industry-wide around ASC 606 in 2018).
# First candidate that has data wins; a company that never tagged any of a
# key's candidates simply omits that key from the result.
FUNDAMENTAL_CONCEPTS = {
    'assets':               [('us-gaap', 'Assets')],
    'liabilities':          [('us-gaap', 'Liabilities')],
    'stockholdersEquity':   [('us-gaap', 'StockholdersEquity')],
    'revenue':              [('us-gaap', 'Revenues'),
                              ('us-gaap', 'RevenueFromContractWithCustomerExcludingAssessedTax'),
                              ('us-gaap', 'RevenueFromContractWithCustomerIncludingAssessedTax')],
    'netIncome':            [('us-gaap', 'NetIncomeLoss')],
    'operatingIncome':      [('us-gaap', 'OperatingIncomeLoss')],
    'epsDiluted':           [('us-gaap', 'EarningsPerShareDiluted')],
    'cash':                 [('us-gaap', 'CashAndCashEquivalentsAtCarryingValue')],
    'longTermDebt':         [('us-gaap', 'LongTermDebtNoncurrent'),
                              ('us-gaap', 'LongTermDebt')],
    'researchAndDevelopment': [('us-gaap', 'ResearchAndDevelopmentExpense')],
    'buybackSpend':         [('us-gaap', 'PaymentsForRepurchaseOfCommonStock')],
    'dividendPerShare':     [('us-gaap', 'CommonStockDividendsPerShareDeclared')],
}

_cik_map_cache: dict | None = None


def _load_cik_map() -> dict:
    global _cik_map_cache
    if _cik_map_cache is not None:
        return _cik_map_cache
    response = requests.get(TICKER_MAP_URL, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    data = response.json()  # {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    _cik_map_cache = {
        entry['ticker'].upper(): str(entry['cik_str']).zfill(10)
        for entry in data.values()
    }
    return _cik_map_cache


def get_cik(ticker: str) -> str | None:
    return _load_cik_map().get(ticker.upper())


def _fetch_concept_series(cik: str, taxonomy: str, tag: str) -> list[dict] | None:
    """
    Single XBRL concept's full filing history for a CIK, deduped by as-of date
    (a later filing for the same date wins — amendments/restatements). Returns
    None when the company never tagged this concept (404) — distinct from an
    empty list, so callers can fall through to the next tag candidate.
    """
    url = CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)
    response = requests.get(url, headers=_HEADERS, timeout=15)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()

    by_date: dict[str, dict] = {}
    filed_by_date: dict[str, str] = {}
    for unit_entries in payload.get('units', {}).values():
        for entry in unit_entries:
            end_date = entry.get('end')
            val = entry.get('val')
            filed = entry.get('filed', '')
            if not end_date or val is None:
                continue
            if end_date not in filed_by_date or filed >= filed_by_date[end_date]:
                by_date[end_date] = {'date': end_date, 'value': val, 'form': entry.get('form', ''), 'filed': filed}
                filed_by_date[end_date] = filed

    return [by_date[d] for d in sorted(by_date)]


def fetch_shares_history(ticker: str) -> list[dict]:
    """
    Returns [{'date': 'YYYY-MM-DD', 'shares': int}, ...] sorted ascending,
    sourced from SEC XBRL company filings. Empty list when the ticker has no
    CIK (not SEC-registered — foreign issuer, crypto, etc.) or no filings under
    either concept. Raises only on network/HTTP failure, never on "no data".
    """
    cik = get_cik(ticker)
    if not cik:
        return []

    for taxonomy, tag in _SHARES_CONCEPTS:
        series = _fetch_concept_series(cik, taxonomy, tag)
        if series:
            return [{'date': e['date'], 'shares': int(e['value'])} for e in series]
    return []


def fetch_fundamentals(ticker: str) -> dict:
    """
    Returns {conceptKey: [{'date','value','form','filed'}, ...]} for the
    curated FUNDAMENTAL_CONCEPTS set. Concepts the company never tagged under
    any of its candidate tags are omitted from the result entirely — callers
    should treat a missing key as "not reported", not zero.

    Merges ALL candidate tags per concept (not just the first that returns
    data) — filers rename tags over time (e.g. revenue recognition tags
    changed industry-wide with ASC 606 in ~2018), so the old and new tag each
    cover a different date range for the same company. Picking only the first
    non-empty candidate silently truncates the series to whichever tag was
    tried first, even though a later tag would extend it with recent years.
    """
    cik = get_cik(ticker)
    if not cik:
        return {}

    result = {}
    for concept_key, tag_candidates in FUNDAMENTAL_CONCEPTS.items():
        by_date: dict[str, dict] = {}
        filed_by_date: dict[str, str] = {}
        for taxonomy, tag in tag_candidates:
            series = _fetch_concept_series(cik, taxonomy, tag)
            if not series:
                continue
            for entry in series:
                d = entry['date']
                if d not in filed_by_date or entry['filed'] >= filed_by_date[d]:
                    by_date[d] = entry
                    filed_by_date[d] = entry['filed']
        if by_date:
            result[concept_key] = [by_date[d] for d in sorted(by_date)]
    return result
