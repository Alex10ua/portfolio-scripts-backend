"""
SEC EDGAR XBRL provider — historical data backfill for US-listed tickers.
Free, no API key. SEC requires a descriptive User-Agent identifying the app +
contact (https://www.sec.gov/os/webmaster-faq#developers) — requests without
one get 403'd.

Every request goes through _sec_get(), which enforces a process-wide rate limit
(SEC's fair-access policy caps clients at 10 requests/second and blocks IPs that
sustain more). One ticker's fundamentals alone costs ~14 requests, so a bulk run
would blow that limit in the first second without this gate. The default here is
much slower than SEC allows — see SECONDS_PER_REQUEST.
"""
import os
import threading
import time
from datetime import datetime, timedelta

import requests

TICKER_MAP_URL = 'https://www.sec.gov/files/company_tickers.json'
CONCEPT_URL = 'https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json'

USER_AGENT = os.getenv('SEC_USER_AGENT', 'FinancePortfolio admin@financeportfolio.local')
_HEADERS = {'User-Agent': USER_AGENT}

# Seconds to wait between SEC requests. Default 5 s (0.2 req/s) — far below SEC's
# documented 10 req/s ceiling, which is where blocking starts rather than a target.
# The cost is real: one ticker's fundamentals is ~15 requests, so ~75 s per ticker.
# Override with SEC_SECONDS_PER_REQUEST; 0 disables throttling entirely.
SECONDS_PER_REQUEST = float(os.getenv('SEC_SECONDS_PER_REQUEST', '5'))
_MIN_INTERVAL = max(0.0, SECONDS_PER_REQUEST)
MAX_REQUESTS_PER_SEC = 1.0 / _MIN_INTERVAL if _MIN_INTERVAL > 0 else 0.0

_rate_lock = threading.Lock()
_last_request_at = 0.0


class SecThrottled(RuntimeError):
    """
    SEC answered 403/429 — the client is being rate-limited or blocked.
    Callers running in bulk must ABORT rather than continue: hammering through a
    403 is what turns a temporary throttle into a longer IP block.
    """


def _throttle() -> None:
    """
    Space out SEC requests process-wide. The sleep happens while holding the
    lock on purpose: concurrent threads must queue behind each other, otherwise
    N workers each honour the interval individually and the real rate is N×.
    """
    global _last_request_at
    if _MIN_INTERVAL <= 0:
        return
    with _rate_lock:
        wait = _last_request_at + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _sec_get(url: str) -> requests.Response:
    """Rate-limited GET against SEC. Raises SecThrottled on 403/429."""
    _throttle()
    response = requests.get(url, headers=_HEADERS, timeout=15)
    if response.status_code in (403, 429):
        raise SecThrottled(
            f'SEC returned {response.status_code} for {url} — rate limited or blocked. '
            f'Raise SEC_SECONDS_PER_REQUEST (currently {SECONDS_PER_REQUEST}s between requests) '
            f'and check SEC_USER_AGENT identifies the app with a contact address.'
        )
    return response

# Point-in-time share counts, best first. Both are as-of-date facts, so they are
# the honest answer to "how many shares existed then".
_SHARES_CONCEPTS = [
    ('us-gaap', 'CommonStockSharesOutstanding'),
    ('dei', 'EntityCommonStockSharesOutstanding'),
]

# Used only when the concepts above come back sparse or stale. A multi-class
# filer (e.g. MA, GOOG, BRK) tags its cover-page and balance-sheet share counts
# PER SHARE CLASS — dimensioned facts, which the companyconcept API does not
# return — so the un-dimensioned series dries up the year the second class
# appears. MA: us-gaap 404s outright, dei stops after 2010-10-27, 4 facts total.
# Weighted-average basic shares stay un-dimensioned (EPS is computed on the
# combined classes) and are filed every quarter — 74 periods for MA, 2007→2026.
# It is an average over the period, not an as-of count, so it lands within a
# fraction of a percent of the true figure but is not exact; point-in-time
# concepts always win a date conflict.
_SHARES_FALLBACK_CONCEPTS = [
    ('us-gaap', 'CommonStockSharesIssued'),
    ('us-gaap', 'WeightedAverageNumberOfSharesOutstandingBasic'),
]

# Below this many point-in-time facts the series is treated as unusable and the
# fallback concepts are fetched too (2 extra SEC requests for that ticker).
_SHARES_SPARSE_BELOW = 8
_SHARES_STALE_DAYS = 730

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
    response = _sec_get(TICKER_MAP_URL)
    response.raise_for_status()
    data = response.json()  # {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    _cik_map_cache = {
        entry['ticker'].upper(): str(entry['cik_str']).zfill(10)
        for entry in data.values()
    }
    return _cik_map_cache


def get_cik(ticker: str) -> str | None:
    return _load_cik_map().get(ticker.upper())


def _period_days(entry: dict) -> int:
    """Length of a duration fact in days; 0 for an instant fact (no 'start')."""
    start, end = entry.get('start'), entry.get('end')
    if not start or not end:
        return 0
    try:
        return (datetime.strptime(end, '%Y-%m-%d') - datetime.strptime(start, '%Y-%m-%d')).days
    except ValueError:
        return 0


def _fetch_concept_series(cik: str, taxonomy: str, tag: str, prefer_shortest_period: bool = False) -> list[dict] | None:
    """
    Single XBRL concept's full filing history for a CIK, deduped by as-of date
    (a later filing for the same date wins — amendments/restatements). Returns
    None when the company never tagged this concept (404) — distinct from an
    empty list, so callers can fall through to the next tag candidate.

    `prefer_shortest_period` matters for duration facts, where one 'end' carries
    both a quarterly and an annual value (Q4 and FY share 12-31). Deduping on
    the date alone then picks whichever was filed later — an annual figure
    landing in an otherwise quarterly series. On it wins the shortest period, so
    the series stays one consistent cadence.
    """
    url = CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)
    response = _sec_get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()

    by_date: dict[str, dict] = {}
    rank_by_date: dict[str, tuple] = {}
    for unit_entries in payload.get('units', {}).values():
        for entry in unit_entries:
            end_date = entry.get('end')
            val = entry.get('val')
            filed = entry.get('filed', '')
            if not end_date or val is None:
                continue
            # sort key, highest wins: shorter period first when asked, then later filing
            rank = (-_period_days(entry), filed) if prefer_shortest_period else (0, filed)
            if end_date not in rank_by_date or rank >= rank_by_date[end_date]:
                by_date[end_date] = {'date': end_date, 'value': val, 'form': entry.get('form', ''), 'filed': filed}
                rank_by_date[end_date] = rank

    return [by_date[d] for d in sorted(by_date)]


def _is_sparse(by_date: dict) -> bool:
    """True when a share series is too short or too old to stand on its own."""
    if len(by_date) < _SHARES_SPARSE_BELOW:
        return True
    newest = max(by_date)
    return newest < (datetime.now() - timedelta(days=_SHARES_STALE_DAYS)).strftime('%Y-%m-%d')


def fetch_shares_history(ticker: str) -> list[dict]:
    """
    Returns [{'date': 'YYYY-MM-DD', 'shares': int}, ...] sorted ascending,
    sourced from SEC XBRL company filings. Empty list when the ticker has no
    CIK (not SEC-registered — foreign issuer, crypto, etc.) or no filings under
    any concept. Raises only on network/HTTP failure, never on "no data".

    Concepts are MERGED, not first-match: filers drift between tags over the
    years, so each covers a different slice of the history and stopping at the
    first non-empty one truncates the series to whichever was tried first.
    Earlier concepts win a date conflict, so the sparse fallbacks can only add
    dates the point-in-time concepts never covered.
    """
    cik = get_cik(ticker)
    if not cik:
        return []

    by_date: dict[str, int] = {}

    def absorb(concepts):
        for taxonomy, tag in concepts:
            series = _fetch_concept_series(cik, taxonomy, tag, prefer_shortest_period=True)
            for entry in series or []:
                by_date.setdefault(entry['date'], int(entry['value']))

    absorb(_SHARES_CONCEPTS)
    # 2 extra SEC requests, so only for filers whose un-dimensioned series is
    # unusable — the multi-share-class case (see _SHARES_FALLBACK_CONCEPTS)
    if _is_sparse(by_date):
        absorb(_SHARES_FALLBACK_CONCEPTS)

    return [{'date': d, 'shares': by_date[d]} for d in sorted(by_date)]


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
