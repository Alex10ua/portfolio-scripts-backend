import os
import finnhub
from datetime import datetime, date


def fetch_market_data(ticker: str) -> dict:
    """
    Fetch full market data from Finnhub API (US tickers only).
    Requires FINNHUB_API_KEY env variable.
    """
    api_key = os.getenv('FINNHUB_API_KEY')
    if not api_key:
        raise RuntimeError('FINNHUB_API_KEY is not set')

    client = finnhub.Client(api_key=api_key)

    quote = client.quote(ticker)
    profile = client.company_profile2(symbol=ticker)

    date_from = '2024-01-01'
    date_to = date.today().isoformat()
    raw_dividends = client.stock_dividends(ticker, _from=date_from, to=date_to) or []
    raw_splits = client.stock_splits(ticker, _from=date_from, to=date_to) or []

    return _map_response(quote, profile, raw_dividends, raw_splits)


def _map_response(quote: dict, profile: dict, raw_dividends: list, raw_splits: list) -> dict:
    dividends = [
        {'dividendDate': d.get('payDate', ''), 'dividendAmount': d.get('amount', '')}
        for d in raw_dividends
        if isinstance(d, dict)
    ]

    splits = [
        {'splitDate': s.get('date', ''), 'ratioSplit': _split_ratio(s)}
        for s in raw_splits
        if isinstance(s, dict)
    ]

    yearly_dividend = _calc_yearly_dividend(raw_dividends)
    last_dividend = raw_dividends[0].get('payDate', '') if raw_dividends else ''

    return {
        'name': profile.get('name', ''),
        'price': quote.get('c') or '',
        'priceYesterday': quote.get('pc') or '',
        'currency': profile.get('currency', ''),
        'yearlyDividend': yearly_dividend,
        'lastDividendPayment': last_dividend,
        'dividends': dividends,
        'splits': splits,
        'country': profile.get('country', ''),
        'sector': profile.get('finnhubIndustry', ''),
        'industry': profile.get('finnhubIndustry', ''),
        'updatedAt': datetime.now(),
    }


def _calc_yearly_dividend(raw_dividends: list) -> float | str:
    if not raw_dividends:
        return ''
    today = date.today()
    cutoff = date(today.year - 1, today.month, today.day).isoformat()
    total = sum(
        d.get('amount', 0) or 0
        for d in raw_dividends
        if isinstance(d, dict) and (d.get('payDate') or '') >= cutoff
    )
    return total if total else ''


def _split_ratio(s: dict) -> str:
    to_factor = s.get('toFactor')
    from_factor = s.get('fromFactor')
    if to_factor and from_factor:
        return f'{to_factor}/{from_factor}'
    return ''
