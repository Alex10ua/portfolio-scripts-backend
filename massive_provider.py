import os
import requests
from datetime import datetime


def fetch_market_data(ticker: str) -> dict:
    """
    Fetch full market data from massive.com REST API.
    Requires MASSIVE_API_KEY and MASSIVE_BASE_URL env variables.
    Raises on HTTP error or missing API key.
    """
    base_url = os.getenv('MASSIVE_BASE_URL')
    api_key = os.getenv('MASSIVE_API_KEY')

    if not api_key:
        raise RuntimeError('MASSIVE_API_KEY is not set')
    if not base_url:
        raise RuntimeError('MASSIVE_BASE_URL is not set')

    url = f"{base_url}/quotes/{ticker}"
    response = requests.get(url, headers={'X-API-Key': api_key}, timeout=10)
    response.raise_for_status()

    return _map_response(ticker, response.json())


def _map_response(ticker: str, data: dict) -> dict:
    """Map massive.com response fields to the internal MarketData schema."""
    raw_dividends = data.get('dividends') or []
    raw_splits = data.get('splits') or []

    dividends = [
        {'dividendDate': d.get('dividendDate', ''), 'dividendAmount': d.get('dividendAmount', '')}
        for d in raw_dividends
    ] if isinstance(raw_dividends, list) else []

    splits = [
        {'splitDate': s.get('splitDate', ''), 'ratioSplit': s.get('ratioSplit', '')}
        for s in raw_splits
    ] if isinstance(raw_splits, list) else []

    return {
        'name': data.get('name') or data.get('companyName') or '',
        'price': data.get('price') or '',
        'priceYesterday': data.get('priceYesterday') or data.get('previousClose') or '',
        'yearlyDividend': data.get('yearlyDividend') or '',
        'lastDividendPayment': data.get('lastDividendPayment') or '',
        'dividends': dividends,
        'splits': splits,
        'currency': data.get('currency') or '',
        'country': data.get('country') or '',
        'sector': data.get('sector') or '',
        'industry': data.get('industry') or '',
        'sharesOutstanding': data.get('sharesOutstanding'),
        'updatedAt': datetime.now(),
    }
