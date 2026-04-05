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

load_dotenv()

# MongoDB connection
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://root:secret@mongodb:27017/')
client = MongoClient(MONGO_URI)
db = client['portfolio']
collection = db['marketData']
tickers_collection = db['tickers']

# Per-provider debounce state
debounce_timers = {"yahoo": None, "massive": None, "auto": None}
locks = {"yahoo": threading.Lock(), "massive": threading.Lock(), "auto": threading.Lock()}

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
        return ticker, operation, None
    except Exception as e:
        return ticker, None, str(e)


def insert_or_update_market_data(ticker: str, provider_fn) -> dict:
    """
    Immediate single-ticker update using the given provider.
    Returns a dict with success status and message/error.
    """
    ticker_result, operation, error = process_ticker(ticker, provider_fn)

    if operation:
        try:
            result = collection.bulk_write([operation])
            if result.matched_count > 0:
                msg = f"Updated data for {ticker}"
            else:
                msg = f"Inserted new data for {ticker}"
            print(msg)
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

#need to find better provider for US ticker than massive and finnhub beacause they not free friendly
def auto_fetch_market_data(ticker: str) -> dict:
    """Route to Finnhub for US tickers (no dot suffix), yfinance for non-US."""
    if '.' in ticker:
        return yahoo_fetch_market_data(ticker)
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


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
