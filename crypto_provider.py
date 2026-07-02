import requests
from datetime import datetime

COINGECKO_BASE = 'https://api.coingecko.com/api/v3'

# symbol -> CoinGecko coin id, filled lazily (search endpoint is rate-limited)
_coin_id_cache: dict[str, str] = {}


def _symbol(ticker: str) -> str:
    """Extract the coin symbol: 'BTC-USD' -> 'btc', 'BTC' -> 'btc'."""
    return ticker.split('-')[0].lower()


def _coin_id(ticker: str) -> str:
    """Resolve a ticker to a CoinGecko coin id (e.g. 'BTC-USD' -> 'bitcoin')."""
    sym = _symbol(ticker)
    if sym in _coin_id_cache:
        return _coin_id_cache[sym]

    resp = requests.get(f'{COINGECKO_BASE}/search', params={'query': sym}, timeout=10)
    resp.raise_for_status()
    coins = resp.json().get('coins') or []
    # exact symbol match first — search ranks by market cap, so [0] is the real coin
    match = next((c for c in coins if (c.get('symbol') or '').lower() == sym), None)
    if match is None:
        raise RuntimeError(f'No CoinGecko coin found for ticker {ticker}')
    _coin_id_cache[sym] = match['id']
    return match['id']


def fetch_market_data(ticker: str) -> dict:
    """
    Fetch market data for a crypto ticker from CoinGecko (no API key required).
    Returns the same schema as the Yahoo provider so callers can use it interchangeably.
    """
    coin_id = _coin_id(ticker)
    resp = requests.get(
        f'{COINGECKO_BASE}/coins/{coin_id}',
        params={
            'localization': 'false', 'tickers': 'false', 'market_data': 'true',
            'community_data': 'false', 'developer_data': 'false', 'sparkline': 'false',
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    market = data.get('market_data') or {}

    price = (market.get('current_price') or {}).get('usd')
    change_pct = market.get('price_change_percentage_24h')
    price_yesterday = None
    if price is not None and change_pct is not None:
        price_yesterday = round(price / (1 + change_pct / 100), 8)

    supply = market.get('circulating_supply')

    return {
        'name': data.get('name') or ticker,
        'price': price,
        'currency': 'USD',
        'priceYesterday': price_yesterday,
        'yearlyDividend': None,
        'lastDividendPayment': None,
        'dividends': [],
        'splits': [],
        'country': None,
        'sector': 'Crypto',
        'industry': 'Cryptocurrency',
        'sharesOutstanding': int(supply) if supply else None,  # circulating supply
        'updatedAt': datetime.now(),
    }
