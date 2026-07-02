from flask import Flask, jsonify, request
from dotenv import load_dotenv
import yfinance as yf
from pymongo import MongoClient, UpdateOne, DeleteOne
from datetime import datetime
import threading
import concurrent.futures
import os

import updateMarketDataUtilities
import massive_provider
import finnhub_provider
import ecb_provider
import crypto_provider

load_dotenv()

# MongoDB connection
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://root:secret@mongodb:27017/')
client = MongoClient(MONGO_URI)
db = client['portfolio']
collection = db['marketData']
tickers_collection = db['tickers']
price_history_collection = db['priceHistoryCache']
custom_assets_collection = db['customAssets']
holdings_collection = db['holdings']


def get_custom_tickers() -> set:
    """
    User-defined CUSTOM asset tickers (customAssets collection). Market providers must
    never update these: their marketData docs hold user-set prices, and names can
    collide with real exchange tickers (e.g. custom "S" vs SentinelOne).
    """
    return set(
        doc['ticker'] for doc in custom_assets_collection.find({}, {'ticker': 1}) if doc.get('ticker')
    )

# Per-provider debounce state
debounce_timers = {"yahoo": None, "massive": None, "auto": None, "crypto": None}
locks = {"yahoo": threading.Lock(), "massive": threading.Lock(),
         "auto": threading.Lock(), "crypto": threading.Lock()}

app = Flask(__name__)


def yahoo_fetch_market_data(ticker: str) -> dict:
    """Fetch full market data from Yahoo Finance."""
    stock = yf.Ticker(ticker)
    info = stock.info
    return {
        'name': updateMarketDataUtilities.get_company_name(info, ticker),
        'price': updateMarketDataUtilities.get_current_price(info, ticker),
        'currency': updateMarketDataUtilities.get_currency(info, ticker),
        'priceYesterday': updateMarketDataUtilities.get_close_price(info, ticker),
        'yearlyDividend': updateMarketDataUtilities.get_yearly_dividend(info, ticker),
        'lastDividendPayment': updateMarketDataUtilities.get_last_dividend_payment(info, ticker),
        'dividends': updateMarketDataUtilities.get_dividends(stock.dividends, ticker),
        'splits': updateMarketDataUtilities.get_splits(stock.splits, ticker),
        'country': updateMarketDataUtilities.get_stock_country(info, ticker),
        'sector': updateMarketDataUtilities.get_sector(info, ticker),
        'industry': updateMarketDataUtilities.get_industry(info, ticker),
        'sharesOutstanding': updateMarketDataUtilities.get_shares_outstanding(info, ticker),
        'updatedAt': datetime.now(),
    }


def _merge_list(existing: list, new: list, key: str) -> list:
    """Merge two lists of dicts, deduplicating by key. New entries overwrite old on conflict."""
    merged = {item[key]: item for item in existing if item.get(key)}
    for item in new:
        if item.get(key):
            merged[item[key]] = item
    return list(merged.values())


def process_ticker(ticker: str, provider_fn) -> tuple[str, UpdateOne | None, str | None]:
    """
    Fetches data for a single ticker using the given provider and returns the update operation.
    Returns: (ticker, update_op, error_message)
    """
    try:
        market_data = provider_fn(ticker)
        existing = collection.find_one({'ticker': ticker}, {'dividends': 1, 'splits': 1}) or {}
        market_data['dividends'] = _merge_list(
            existing.get('dividends') or [], market_data.get('dividends') or [], 'dividendDate'
        )
        market_data['splits'] = _merge_list(
            existing.get('splits') or [], market_data.get('splits') or [], 'splitDate'
        )
        operation = UpdateOne(
            {'ticker': ticker},
            {'$set': market_data},
            upsert=True,
        )
        get_price_history(ticker)
        return ticker, operation, None
    except Exception as e:
        return ticker, None, str(e)


def insert_or_update_market_data(ticker: str, provider_fn) -> dict:
    """
    Immediate single-ticker update using the given provider.
    Returns a dict with success status and message/error.
    """
    if ticker in get_custom_tickers():
        msg = f"Skipping {ticker}: custom asset ticker, market data only updated for stock/crypto"
        print(msg)
        return {"success": False, "error": msg}

    ticker_result, operation, error = process_ticker(ticker, provider_fn)

    if operation:
        try:
            result = collection.bulk_write([operation])
            if result.matched_count > 0:
                msg = f"Updated data for {ticker}"
            else:
                msg = f"Inserted new data for {ticker}"
            print(msg)
            get_price_history(ticker)
            return {"success": True, "message": msg}
        except Exception as e:
            error_msg = f"Error writing to DB for {ticker}: {e}"
            print(error_msg)
            return {"success": False, "error": error_msg}
    else:
        error_msg = f"Failed to fetch data for {ticker}: {error}"
        print(error_msg)
        return {"success": False, "error": error_msg}


def run_task(provider_fn, provider_key: str):
    global debounce_timers
    with locks[provider_key]:
        debounce_timers[provider_key] = None

    print(f"[{datetime.now()}] Starting batch processing task (provider={provider_key})...")

    cursor = tickers_collection.find({}, {'ticker': 1})
    ticker_symbols: list[str] = list(set(str(doc.get('ticker')) for doc in cursor if doc.get('ticker')))

    # Custom asset tickers never get provider updates — drop them from the queue unprocessed.
    custom_tickers = get_custom_tickers()
    skipped_custom = [t for t in ticker_symbols if t in custom_tickers]
    if skipped_custom:
        ticker_symbols = [t for t in ticker_symbols if t not in custom_tickers]
        tickers_collection.delete_many({'ticker': {'$in': skipped_custom}})
        print(f"Skipped {len(skipped_custom)} custom asset tickers: {skipped_custom}")

    total = len(ticker_symbols)
    if total == 0:
        print("No tickers to process.")
        return

    print(f"Found {total} unique tickers to process.")

    updates = []
    deletes = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {executor.submit(process_ticker, t, provider_fn): t for t in ticker_symbols}  # type: ignore

        for i, future in enumerate(concurrent.futures.as_completed(future_to_ticker), 1):
            ticker = future_to_ticker[future]
            try:
                processed_ticker, operation, error = future.result()

                if operation:
                    updates.append(operation)
                    deletes.append(DeleteOne({'ticker': processed_ticker}))
                    print(f"[{i}/{total}] Successfully fetched: {processed_ticker}")
                else:
                    print(f"[{i}/{total}] Failed to fetch: {processed_ticker} - Error: {error}")

            except Exception as e:
                print(f"[{i}/{total}] Exception processing {ticker}: {e}")

    if updates:
        try:
            print(f"Writing {len(updates)} updates to 'marketData' collection...")
            result = collection.bulk_write(updates)
            print(f"Bulk write result: Matched={result.matched_count}, Modified={result.modified_count}, Upserted={result.upserted_count}")
        except Exception as e:
            print(f"Error during bulk update: {e}")

    if deletes:
        try:
            print(f"Removing {len(deletes)} processed tickers from 'tickers' queue...")
            result = tickers_collection.bulk_write(deletes)
            print(f"Bulk delete result: Deleted={result.deleted_count}")
        except Exception as e:
            print(f"Error during bulk delete: {e}")

    print(f"[{datetime.now()}] Task finished (provider={provider_key}).")


def schedule_batch(provider_key: str, provider_fn):
    """Schedule a debounced batch run for the given provider."""
    with locks[provider_key]:
        if debounce_timers[provider_key] is not None:
            debounce_timers[provider_key].cancel()
        timer = threading.Timer(10, run_task, args=[provider_fn, provider_key])
        debounce_timers[provider_key] = timer
        timer.start()


def _handle_update(provider_key: str, provider_fn):
    """Shared handler for both update endpoints."""
    data = request.get_json(silent=True) or {}
    ticker = data.get('ticker')

    if ticker:
        result = insert_or_update_market_data(ticker, provider_fn)
        if result["success"]:
            return jsonify({"status": "success", "message": result["message"], "ticker": ticker}), 200
        else:
            return jsonify({"status": "error", "error": result["error"], "ticker": ticker}), 500
    else:
        print(f'Batch update triggered (provider={provider_key})')
        schedule_batch(provider_key, provider_fn)
        return jsonify({"status": "debounced", "message": f"{provider_key.capitalize()} batch update scheduled in 10 seconds"}), 202

def is_crypto_ticker(ticker: str) -> bool:
    """True when any holding marks this ticker as CRYPTO."""
    return holdings_collection.find_one({'ticker': ticker, 'assetType': 'CRYPTO'}) is not None


def yahoo_crypto_symbol(ticker: str) -> str:
    """yfinance needs pair form for crypto: 'BTC' -> 'BTC-USD'; 'BTC-EUR' and stocks pass through."""
    if is_crypto_ticker(ticker) and '-' not in ticker:
        return f'{ticker}-USD'
    return ticker


#need to find better provider for US ticker than massive and finnhub beacause they not free friendly
def auto_fetch_market_data(ticker: str) -> dict:
    """Route by asset type: CRYPTO holdings -> CoinGecko, everything else -> Yahoo Finance."""
    if is_crypto_ticker(ticker):
        return crypto_provider.fetch_market_data(ticker)
    return yahoo_fetch_market_data(ticker)


@app.route('/update/yahoo', methods=['POST'])
def update_yahoo():
    return _handle_update("yahoo", yahoo_fetch_market_data)


@app.route('/update/auto', methods=['POST'])
def update_auto():
    return _handle_update("auto", auto_fetch_market_data)


@app.route('/update/massive', methods=['POST'])
def update_massive():
    return _handle_update("massive", massive_provider.fetch_market_data)


@app.route('/update/crypto', methods=['POST'])
def update_crypto():
    return _handle_update("crypto", crypto_provider.fetch_market_data)


def get_price_history(ticker: str) -> bool:
    """Fetch full price history from yfinance and store it in MongoDB. Returns True on success."""
    try:
        stock = yf.Ticker(yahoo_crypto_symbol(ticker))
        hist = stock.history(period='max')
        if hist.empty:
            return False
        entries = [
            {'date': str(idx.date()), 'price': round(float(row['Close']), 4)}
            for idx, row in hist.iterrows()
        ]
        price_history_collection.update_one(
            {'_id': ticker},
            {'$set': {'ticker': ticker, 'history': entries, 'lastUpdated': datetime.now().strftime('%Y-%m-%d')}},
            upsert=True,
        )
        return True
    except Exception as e:
        print(f'[price_history] Error fetching {ticker}: {e}')
        return False


@app.route('/update/exchangeRates', methods=['POST'])
def update_exchange_rates():
    try:
        rates = ecb_provider.fetch_and_store_rates(db)
        return jsonify({'status': 'ok', 'currencies': list(rates.keys())}), 200
    except Exception as e:
        print(f'[exchangeRates] Error: {e}')
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/history/refresh/<ticker>', methods=['POST'])
def refresh_price_history(ticker):
    if ticker in get_custom_tickers():
        return jsonify({'status': 'skipped', 'reason': 'custom asset ticker', 'ticker': ticker}), 200
    success = get_price_history(ticker)
    if success:
        return jsonify({'status': 'ok', 'ticker': ticker}), 200
    return jsonify({'status': 'error', 'ticker': ticker}), 500


def fetch_shares_outstanding(ticker: str):
    """Fetch only sharesOutstanding for a ticker from Yahoo Finance."""
    stock = yf.Ticker(ticker)
    return updateMarketDataUtilities.get_shares_outstanding(stock.info, ticker)


@app.route('/update/sharesOutstanding', methods=['POST'])
def update_shares_outstanding():
    """
    Backfill sharesOutstanding: single ticker via {"ticker": "..."} body,
    or every existing marketData doc when body is empty.
    Unknown tickers get a full new marketData doc (which includes sharesOutstanding).
    """
    data = request.get_json(silent=True) or {}
    ticker = data.get('ticker')
    if ticker:
        tickers = [ticker]
    else:
        tickers = list(set(
            doc['ticker'] for doc in collection.find({}, {'ticker': 1}) if doc.get('ticker')
        ))

    # custom asset tickers hold user-set data — never fetch provider data for them
    custom_tickers = get_custom_tickers()
    skipped = [t for t in tickers if t in custom_tickers]
    tickers = [t for t in tickers if t not in custom_tickers]

    existing = set(
        doc['ticker'] for doc in collection.find({'ticker': {'$in': tickers}}, {'ticker': 1})
    )

    def backfill_one(t: str) -> str:
        """Returns 'updated' | 'created' | 'failed'."""
        if t in existing:
            if is_crypto_ticker(t):
                shares = crypto_provider.fetch_market_data(t).get('sharesOutstanding')
            else:
                shares = fetch_shares_outstanding(t)
            if not shares:
                return 'failed'
            collection.update_one({'ticker': t}, {'$set': {'sharesOutstanding': shares}})
            return 'updated'
        # ticker not in marketData yet — create the full doc, sharesOutstanding included
        result = insert_or_update_market_data(t, auto_fetch_market_data)
        return 'created' if result['success'] else 'failed'

    updated, created, failed = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {executor.submit(backfill_one, t): t for t in tickers}
        for future in concurrent.futures.as_completed(future_to_ticker):
            t = future_to_ticker[future]
            try:
                outcome = future.result()
            except Exception as e:
                print(f'[sharesOutstanding] Error processing {t}: {e}')
                outcome = 'failed'
            {'updated': updated, 'created': created, 'failed': failed}[outcome].append(t)

    print(f'[sharesOutstanding] updated={len(updated)} created={len(created)} '
          f'skippedCustom={len(skipped)} failed={len(failed)}')
    return jsonify({
        'status': 'ok',
        'requested': len(tickers),
        'updated': len(updated),
        'created': created,
        'skippedCustom': skipped,
        'failed': failed,
    }), 200


def get_monthly_price_history(ticker: str) -> bool:
    """Fetch monthly price history from yfinance and store in MongoDB. Returns True on success."""
    try:
        stock = yf.Ticker(yahoo_crypto_symbol(ticker))
        hist = stock.history(period='max', interval='1mo')
        if hist.empty:
            return False
        entries = [
            {'date': str(idx.date())[:7], 'price': round(float(row['Close']), 4)}
            for idx, row in hist.iterrows()
        ]
        price_history_collection.update_one(
            {'_id': ticker},
            {'$set': {'monthlyHistory': entries, 'lastUpdated': datetime.now().strftime('%Y-%m-%d')}},
            upsert=True,
        )
        return True
    except Exception as e:
        print(f'[monthly_price_history] Error fetching {ticker}: {e}')
        return False


@app.route('/update/full', methods=['POST'])
def update_full():
    """Manually trigger a full update for a ticker: price, dividends, splits, daily history, monthly history."""
    data = request.get_json(silent=True) or {}
    ticker = data.get('ticker')
    if not ticker:
        return jsonify({'status': 'error', 'error': 'ticker required'}), 400

    result = insert_or_update_market_data(ticker, yahoo_fetch_market_data)
    if not result['success']:
        return jsonify({'status': 'error', 'error': result['error'], 'ticker': ticker}), 500

    daily_ok = get_price_history(ticker)
    monthly_ok = get_monthly_price_history(ticker)

    return jsonify({
        'status': 'success',
        'ticker': ticker,
        'marketData': result['message'],
        'dailyHistory': 'ok' if daily_ok else 'failed',
        'monthlyHistory': 'ok' if monthly_ok else 'failed',
    }), 200


# ---------- Swagger UI (manual endpoint testing) ----------

TICKER_BODY = {
    'required': False,
    'content': {
        'application/json': {
            'schema': {
                'type': 'object',
                'properties': {'ticker': {'type': 'string', 'example': 'AAPL'}},
            },
        },
    },
    'description': 'With "ticker": immediate single-ticker update. Without: debounced batch over the tickers queue.',
}

OPENAPI_SPEC = {
    'openapi': '3.0.3',
    'info': {
        'title': 'Portfolio Market Data Service',
        'description': 'Flask service fetching market data (Yahoo Finance, Massive, ECB) into MongoDB.',
        'version': '1.0.0',
    },
    'paths': {
        '/update/yahoo': {
            'post': {
                'summary': 'Update market data via Yahoo Finance',
                'requestBody': TICKER_BODY,
                'responses': {'200': {'description': 'Single ticker updated'},
                              '202': {'description': 'Batch scheduled (10 s debounce)'},
                              '500': {'description': 'Fetch or DB error'}},
            },
        },
        '/update/auto': {
            'post': {
                'summary': 'Update market data via auto-routed provider (CRYPTO holdings -> CoinGecko, else Yahoo)',
                'requestBody': TICKER_BODY,
                'responses': {'200': {'description': 'Single ticker updated'},
                              '202': {'description': 'Batch scheduled (10 s debounce)'},
                              '500': {'description': 'Fetch or DB error'}},
            },
        },
        '/update/massive': {
            'post': {
                'summary': 'Update market data via Massive API',
                'requestBody': TICKER_BODY,
                'responses': {'200': {'description': 'Single ticker updated'},
                              '202': {'description': 'Batch scheduled (10 s debounce)'},
                              '500': {'description': 'Fetch or DB error'}},
            },
        },
        '/update/crypto': {
            'post': {
                'summary': 'Update market data via CoinGecko (crypto tickers, e.g. BTC or BTC-USD)',
                'requestBody': TICKER_BODY,
                'responses': {'200': {'description': 'Single ticker updated'},
                              '202': {'description': 'Batch scheduled (10 s debounce)'},
                              '500': {'description': 'Fetch or DB error'}},
            },
        },
        '/update/full': {
            'post': {
                'summary': 'Full update for one ticker: market data + daily + monthly price history',
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['ticker'],
                        'properties': {'ticker': {'type': 'string', 'example': 'AAPL'}},
                    }}},
                },
                'responses': {'200': {'description': 'Update result per part'},
                              '400': {'description': 'ticker required'},
                              '500': {'description': 'Fetch or DB error'}},
            },
        },
        '/update/sharesOutstanding': {
            'post': {
                'summary': 'Backfill sharesOutstanding (single ticker, or all marketData docs when body empty); unknown tickers get a full new marketData doc created',
                'requestBody': TICKER_BODY,
                'responses': {'200': {'description': '{requested, updated, created[], failed[]}'},
                              '500': {'description': 'DB error'}},
            },
        },
        '/update/exchangeRates': {
            'post': {
                'summary': 'Fetch EUR FX rates from ECB into exchangeRates collection',
                'responses': {'200': {'description': 'Currencies updated'},
                              '500': {'description': 'Fetch error'}},
            },
        },
        '/history/refresh/{ticker}': {
            'post': {
                'summary': 'Refresh full daily price history for a ticker',
                'parameters': [{
                    'name': 'ticker', 'in': 'path', 'required': True,
                    'schema': {'type': 'string'}, 'example': 'AAPL',
                }],
                'responses': {'200': {'description': 'History stored'},
                              '500': {'description': 'Fetch failed or empty history'}},
            },
        },
    },
}

SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Market Data Service — Swagger UI</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: '/apispec.json',
      dom_id: '#swagger-ui',
      tryItOutEnabled: true,
    });
  </script>
</body>
</html>"""


@app.route('/apispec.json')
def apispec():
    return jsonify(OPENAPI_SPEC)


@app.route('/apidocs')
def apidocs():
    return SWAGGER_UI_HTML


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
