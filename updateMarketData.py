from flask import Flask, jsonify, request
from dotenv import load_dotenv
import yfinance as yf
from pymongo import MongoClient, UpdateOne, DeleteOne
from datetime import datetime, timedelta
import threading
import concurrent.futures
import math
import time
import os
import traceback

import updateMarketDataUtilities
import massive_provider
import finnhub_provider
import ecb_provider
import crypto_provider
import sec_edgar_provider

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
shares_history_collection = db['sharesOutstandingHistory']
fundamentals_collection = db['companyFundamentals']


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

# Throttled sequential batch — one run at a time
throttled_lock = threading.Lock()
# Live event feed for the run: the admin panel polls the status endpoint with a
# cursor (?sinceSeq=) and appends whatever is new to its log, so an hour-long run
# reads as it happens instead of as one summary at the end. Capped — these are
# progress lines, not an audit trail; stdout keeps the full record.
THROTTLED_EVENT_CAP = 200
throttled_status = {
    "running": False,
    "scope": None,   # "queue" | "holdings" | "all" — which ticker list the run took
    "total": 0,
    "processed": 0,
    "updated": 0,
    # {ticker, stage, error, at} per entry — a bare ticker list never said whether
    # Yahoo refused it or Mongo did, which is the first thing you ask.
    "failed": [],
    "pauseSeconds": None,
    "currentTicker": None,
    "currentStage": None,   # fetching | saving | waiting | paused
    "lastError": None,
    # Cooperative stop: the worker thread checks this between tickers. Killing a
    # thread mid-write is not an option, so a cancel finishes the ticker in flight.
    "cancelRequested": False,
    # Cooperative pause, same rule: the ticker in flight is finished and written,
    # then the worker parks between tickers until resumed. The run stays "running"
    # (and keeps its slot) while paused, so nothing can start on top of it.
    "paused": False,
    "pausedAt": None,
    "pausedSeconds": 0.0,
    "abortedReason": None,
    "startedAt": None,
    "finishedAt": None,
    "events": [],
    "eventSeq": 0,
}

# Bulk SEC EDGAR backfill — one run at a time (see run_sec_bulk_task)
sec_bulk_lock = threading.Lock()
sec_bulk_status = {
    "running": False,
    "total": 0,
    "processed": 0,
    "fundamentalsUpdated": 0,
    "sharesUpdated": 0,
    "noData": [],
    "failed": [],
    "currentTicker": None,
    "cancelRequested": False,
    "abortedReason": None,
    "startedAt": None,
    "finishedAt": None,
}


def _cancelled(lock, status) -> bool:
    with lock:
        return bool(status["cancelRequested"])


def _sleep_unless_cancelled(seconds: float, lock, status) -> bool:
    """
    Sleep in ≤1 s slices, bailing out as soon as a cancel lands. Without this a
    stop would sit through the whole inter-ticker pause (15 s by default) before
    taking effect. Returns False when cancelled.
    """
    remaining = seconds
    while remaining > 0:
        if _cancelled(lock, status):
            return False
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
    return not _cancelled(lock, status)


def throttled_event(level: str, message: str, ticker: str | None = None) -> None:
    """
    One line of run progress: printed to stdout (the full record) and appended to
    the capped in-memory feed the admin panel streams. level is 'info'|'ok'|'warn'|'err'.
    """
    print(f"[throttled] {message}")
    with throttled_lock:
        throttled_status["eventSeq"] += 1
        throttled_status["events"].append({
            "seq": throttled_status["eventSeq"],
            "at": datetime.now().isoformat(),
            "level": level,
            "ticker": ticker,
            "message": message,
        })
        # Trim oldest first — a caller polling with a cursor gets whatever survives.
        overflow = len(throttled_status["events"]) - THROTTLED_EVENT_CAP
        if overflow > 0:
            del throttled_status["events"][:overflow]


def _wait_while_paused() -> bool:
    """
    Park the worker between tickers while the run is paused, waking every 0.5 s to
    re-check. A cancel beats a pause (a Stop while paused must not hang until the
    user remembers to resume). Returns False when the run should stop.
    Time spent here is accumulated into pausedSeconds so the ETA stays honest.
    """
    paused_from = None
    while True:
        with throttled_lock:
            if throttled_status["cancelRequested"]:
                if paused_from is not None:
                    throttled_status["pausedSeconds"] += time.monotonic() - paused_from
                return False
            if not throttled_status["paused"]:
                if paused_from is not None:
                    throttled_status["pausedSeconds"] += time.monotonic() - paused_from
                return True
            if paused_from is None:
                paused_from = time.monotonic()
                throttled_status["currentStage"] = "paused"
        time.sleep(0.5)

app = Flask(__name__)

# yfinance looks up an exchange's timezone before every history download and caches
# it in an sqlite file. Left at its default the cache lives inside the container, so
# a recreate makes every ticker pay that extra chart request again — measured 2
# requests per history(period='max') cold vs 1 warm. Point it at a volume.
YF_CACHE_DIR = os.getenv('YF_CACHE_DIR')
if YF_CACHE_DIR:
    try:
        os.makedirs(YF_CACHE_DIR, exist_ok=True)
        yf.set_tz_cache_location(YF_CACHE_DIR)
    except Exception as e:  # read-only FS, bad path — the default location still works
        print(f'[yfinance] tz cache location {YF_CACHE_DIR} unusable: {e}')

# One .info costs 3 Yahoo requests (quoteSummary + quote + timeseries), and several
# endpoints want the same payload for the same ticker back to back (statistics, then
# sharesOutstanding). Short TTL so an explicit "refresh" still sees fresh data.
YF_INFO_CACHE_TTL = float(os.getenv('YF_INFO_CACHE_TTL', '300'))
_info_cache: dict[str, tuple[float, dict]] = {}
_info_cache_lock = threading.Lock()


def ticker_info(symbol: str, stock=None) -> dict:
    """Yahoo .info for a symbol, reused within YF_INFO_CACHE_TTL seconds (0 disables)."""
    if YF_INFO_CACHE_TTL > 0:
        with _info_cache_lock:
            entry = _info_cache.get(symbol)
            if entry and (time.monotonic() - entry[0]) < YF_INFO_CACHE_TTL:
                return entry[1]
    info = (stock or yf.Ticker(symbol)).info
    if YF_INFO_CACHE_TTL > 0 and info:
        with _info_cache_lock:
            _info_cache[symbol] = (time.monotonic(), info)
    return info


def actions_series(hist, column: str):
    """
    Non-zero Dividends / Stock Splits column of a history frame. history() already
    returns both columns, while Ticker.dividends/.splits re-request the chart with
    different params and so miss yfinance's cache — same data, one wasted request.
    """
    try:
        if hist is None or column not in hist.columns:
            return {}  # empty mapping — get_dividends/get_splits just yield nothing
        series = hist[column]
        return series[series != 0]
    except Exception as e:
        print(f'[actions] Error reading {column}: {e}')
        return {}


def get_shares_with_fallback(ticker: str, info: dict):
    """
    sharesOutstanding from the ticker's own Yahoo info. IOB GDR listings (".IL",
    e.g. MHPC.IL) carry price but omit sharesOutstanding — fall back to the sibling
    LSE listing (".L", e.g. MHPC.L), which reports it. GDRs are typically 1:1 with
    the underlying, so the sibling's count is the right figure for the ownership math.
    """
    shares = updateMarketDataUtilities.get_shares_outstanding(info, ticker)
    if shares is None and ticker.upper().endswith('.IL'):
        sibling = ticker[:-3] + '.L'
        try:
            shares = updateMarketDataUtilities.get_shares_outstanding(ticker_info(sibling), sibling)
            if shares:
                print(f'[sharesOutstanding] {ticker}: filled from {sibling} = {shares}')
        except Exception as e:
            print(f'[sharesOutstanding] {ticker}: {sibling} fallback failed: {e}')
    return shares


def yahoo_fetch_market_data(ticker: str, request_pause: float = 0) -> dict:
    """
    Fetch full market data from Yahoo Finance in two calls: .info (quoteSummary,
    3 HTTP requests) and one history(period='max') download (1-2, depending on the
    tz cache) that serves dividends, splits AND the daily price history. The history
    DataFrame rides along under '_history' (popped by process_ticker before the DB
    write, reused by get_price_history). request_pause sleeps between the two.

    Dividends/splits are read off that frame rather than via stock.dividends /
    stock.splits — the properties re-request the chart with different params, so
    they miss yfinance's cache and cost an extra request for identical data.
    """
    # Crypto is quoted as a pair on Yahoo ('BTC' -> 'BTC-USD'). Fetching the raw
    # symbol returned nothing and get_price_history then re-fetched the mapped one.
    symbol = yahoo_crypto_symbol(ticker)
    stock = yf.Ticker(symbol)
    info = ticker_info(symbol, stock)
    if request_pause > 0:
        time.sleep(request_pause)
    # auto_adjust=False keeps BOTH columns in the one download: 'Adj Close' (total
    # return, what the stored series has always been) and 'Close' (as quoted).
    # Historical yield needs the unadjusted close — dividing a nominal dividend by a
    # dividend-adjusted price inflates every past yield. Same request count either way.
    hist = stock.history(period='max', auto_adjust=False)
    data = {
        'name': updateMarketDataUtilities.get_company_name(info, ticker),
        'price': updateMarketDataUtilities.get_current_price(info, ticker),
        'currency': updateMarketDataUtilities.get_currency(info, ticker),
        'priceYesterday': updateMarketDataUtilities.get_close_price(info, ticker),
        'yearlyDividend': updateMarketDataUtilities.get_yearly_dividend(info, ticker),
        'lastDividendPayment': updateMarketDataUtilities.get_last_dividend_payment(info, ticker),
        'dividends': updateMarketDataUtilities.get_dividends(actions_series(hist, 'Dividends'), ticker),
        'splits': updateMarketDataUtilities.get_splits(actions_series(hist, 'Stock Splits'), ticker),
        'country': updateMarketDataUtilities.get_stock_country(info, ticker),
        'sector': updateMarketDataUtilities.get_sector(info, ticker),
        'industry': updateMarketDataUtilities.get_industry(info, ticker),
        'sharesOutstanding': get_shares_with_fallback(ticker, info),
        # full key-statistics snapshot (margins, valuation, balance sheet, ...);
        # None when .info carried none of them, so it gets dropped, not written blank
        'statistics': updateMarketDataUtilities.get_statistics(info, ticker),
        'updatedAt': datetime.now(),
    }
    # Downloaded under the mapped symbol, so it is the right series for this ticker
    # either way — get_price_history reuses it instead of downloading again.
    data['_history'] = hist
    return data


def record_shares_history(ticker: str, shares) -> None:
    """
    Append {date, shares} to the ticker's sharesOutstandingHistory doc, but only
    when the value differs from the last recorded one — a sparse time series of
    share-count changes (buybacks/dilution; circulating supply for crypto).
    A second change on the same day replaces that day's entry. Best-effort:
    never raises, so a history failure can't break the marketData write.
    """
    if not shares:
        return
    try:
        shares = int(shares)
        today = datetime.now().strftime('%Y-%m-%d')
        doc = shares_history_collection.find_one({'_id': ticker}, {'history': {'$slice': -1}})
        last_entries = (doc or {}).get('history') or []
        if last_entries:
            last = last_entries[-1]
            if last.get('shares') == shares:
                return
            if last.get('date') == today:
                shares_history_collection.update_one({'_id': ticker}, {'$pop': {'history': 1}})
        shares_history_collection.update_one(
            {'_id': ticker},
            {'$push': {'history': {'date': today, 'shares': shares}},
             '$set': {'ticker': ticker, 'lastUpdated': today}},
            upsert=True,
        )
    except Exception as e:
        print(f'[sharesHistory] Error recording {ticker}: {e}')


def record_shares_history_bulk(ticker: str, entries: list) -> int:
    """
    Merge a batch of {date, shares} entries (e.g. from SEC EDGAR) into the
    ticker's history doc — same collection/shape as record_shares_history, but
    for a whole time series at once. A later-written entry on an existing date
    overwrites it. Best-effort: never raises. Returns entries stored (0 on
    no-op/error) so the caller can report what happened.
    """
    if not entries:
        return 0
    try:
        doc = shares_history_collection.find_one({'_id': ticker}, {'history': 1})
        merged_by_date = {h['date']: h['shares'] for h in (doc or {}).get('history') or [] if h.get('date')}
        for e in entries:
            if e.get('date') and e.get('shares') is not None:
                merged_by_date[e['date']] = int(e['shares'])
        merged = [{'date': d, 'shares': merged_by_date[d]} for d in sorted(merged_by_date)]
        shares_history_collection.update_one(
            {'_id': ticker},
            {'$set': {'history': merged, 'ticker': ticker, 'lastUpdated': merged[-1]['date']}},
            upsert=True,
        )
        return len(merged)
    except Exception as e:
        print(f'[sharesHistory] Error bulk-recording {ticker}: {e}')
        return 0


def _day_key(value) -> str:
    """Normalize a date-ish value (tz-aware Timestamp, naive datetime, or string)
    to a 'YYYY-MM-DD' string so entries from different providers/reads dedup as
    the same day. Mongo returns naive datetimes while yfinance yields tz-aware
    Timestamps — as raw dict keys they never compare equal.

    The day meant is always the *exchange's* calendar day — what
    updateMarketDataUtilities.action_day now writes, and what the Finnhub and
    Massive providers already wrote.

    The legacy branch below bridges to documents written before that: a raw
    tz-aware Timestamp stored as an instant comes back from Mongo naive and in
    UTC, so an exchange ahead of UTC reads a day early (Frankfurt local midnight
    is 22:00 the previous day, London 23:00). Keyed literally, the stored
    '2025-05-04 22:00' never matched the '2025-05-05' being written, and every
    European and UK ticker re-appended its whole dividend and split history on
    each update — BAS.DE, ENI.MI, BN.PA, UKW.L all carried exact duplicates. US
    tickers hid it: their local midnight is 04:00/05:00 UTC on the *same* day,
    which is why the 2026-07-09 fix looked complete. An action is always dated at
    some local midnight, so a stored time of day at or past noon can only be that
    shift, and rolling it forward makes a legacy row collapse into the new
    string-dated one instead of doubling it. Refreshing a ticker therefore heals
    its document; migrate-action-dates.js does the same thing without the fetch.

    corporate_actions._day_key needs no such bridge — it only ever sees freshly
    fetched values, and matches them against ignored_splits.json, whose dates are
    hand-written as the exchange's calendar day.
    """
    if hasattr(value, 'date') and callable(getattr(value, 'date')):
        if getattr(value, 'tzinfo', None) is not None:
            return str(value.date())                 # tz-aware: its own local day
        if getattr(value, 'hour', 0) >= 12:          # legacy: stored instant, read back as UTC
            return str((value + timedelta(days=1)).date())
        return str(value.date())
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return str(value)[:10]


def _merge_list(existing: list, new: list, key: str) -> list:
    """Merge two lists of dicts, deduplicating by calendar day of `key`.
    New entries overwrite old on conflict; pre-existing same-day duplicates
    collapse to the last one."""
    merged = {_day_key(item[key]): item for item in existing if item.get(key)}
    for item in new:
        if item.get(key):
            merged[_day_key(item[key])] = item
    return list(merged.values())


def process_ticker(ticker: str, provider_fn, request_pause: float = 0) -> tuple[str, UpdateOne | None, str | None]:
    """
    Fetches data for a single ticker using the given provider and returns the update operation.
    request_pause sleeps between the provider fetch and the price-history request.
    Returns: (ticker, update_op, error_message)
    """
    try:
        market_data = provider_fn(ticker)
        # DataFrame piggybacked by yahoo_fetch_market_data — not BSON, must not reach $set
        hist = market_data.pop('_history', None)
        existing = collection.find_one({'ticker': ticker}, {'dividends': 1, 'splits': 1}) or {}
        market_data['dividends'] = _merge_list(
            existing.get('dividends') or [], market_data.get('dividends') or [], 'dividendDate'
        )
        market_data['splits'] = _merge_list(
            existing.get('splits') or [], market_data.get('splits') or [], 'splitDate'
        )
        # Don't overwrite existing good data with blanks: a throttled/partial provider
        # response returns '' / None for missing fields (e.g. price, currency,
        # sharesOutstanding). Drop those so $set only writes real values. Merged
        # dividends/splits lists and the datetime updatedAt are never '' / None so stay.
        market_data = {k: v for k, v in market_data.items() if v not in ('', None)}
        record_shares_history(ticker, market_data.get('sharesOutstanding'))
        operation = UpdateOne(
            {'ticker': ticker},
            {'$set': market_data},
            upsert=True,
        )
        if hist is not None:
            get_price_history(ticker, hist)  # reuse download — no new Yahoo request
        else:
            if request_pause > 0:
                time.sleep(request_pause)
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
            # price history already stored by process_ticker — no second fetch
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
    # Optional assetType in the body wins over the holdings lookup: on the very
    # first transaction of a ticker the holding doesn't exist yet, so
    # is_crypto_ticker() would misroute crypto to Yahoo (blank data).
    data = request.get_json(silent=True) or {}
    if str(data.get('assetType') or '').upper() == 'CRYPTO':
        return _handle_update("auto", crypto_provider.fetch_market_data)
    return _handle_update("auto", auto_fetch_market_data)


@app.route('/update/massive', methods=['POST'])
def update_massive():
    return _handle_update("massive", massive_provider.fetch_market_data)


@app.route('/update/crypto', methods=['POST'])
def update_crypto():
    return _handle_update("crypto", crypto_provider.fetch_market_data)


def history_entries(hist) -> list:
    """
    Daily [{date, price, rawPrice}] from a history frame.

    price    — total-return close ('Adj Close' when the frame carries one), the
               series every existing consumer reads; unchanged.
    rawPrice — close as quoted, dividends NOT adjusted out. Only the yield-history
               math wants this: adjusted closes are back-scaled by every dividend
               since, so ttmDividend/adjustedPrice reads years too rich.

    Frames downloaded with auto_adjust=True have no 'Adj Close'; there both fields
    carry the adjusted close, which is what the pre-rawPrice docs already hold.

    Rows with a non-finite close are dropped. Yahoo returns a NaN bar for a day it
    has no close for yet (the current session, a halt), and a NaN survives into
    Mongo as a Double the Java side cannot read at all: BigDecimal has no NaN, so
    every consumer of the ticker's history fails on the document, not just on
    that point.
    """
    adjusted = 'Adj Close' in hist.columns
    entries = []
    for idx, row in hist.iterrows():
        close = _finite(row['Close'])
        price = _finite(row['Adj Close']) if adjusted else close
        if close is None or price is None:
            continue
        entries.append({
            'date': str(idx.date()),
            'price': round(price, 4),
            'rawPrice': round(close, 4),
        })
    return entries


def _finite(value):
    """float(value) when it is a real number, else None (covers NaN, inf, None)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def get_price_history(ticker: str, hist=None) -> bool:
    """
    Store full daily price history in MongoDB. Fetches from yfinance unless a
    pre-downloaded history DataFrame is passed in. Returns True on success.
    """
    try:
        if hist is None:
            stock = yf.Ticker(yahoo_crypto_symbol(ticker))
            hist = stock.history(period='max', auto_adjust=False)
        if hist.empty:
            return False
        entries = history_entries(hist)
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


@app.route('/update/sharesOutstandingHistory', methods=['POST'])
def update_shares_outstanding_history():
    """
    Backfill historical sharesOutstanding for one ticker from SEC EDGAR XBRL
    filings (US-registered companies only — no data for foreign private
    issuers, ADRs without SEC registration, or crypto). Body: {"ticker": "MSFT"}.
    Merges into sharesOutstandingHistory; does NOT touch the live
    marketData.sharesOutstanding field (SEC filings lag the current
    yfinance-sourced value, so overwriting it would make it staler, not fresher).
    """
    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'status': 'error', 'error': 'ticker is required'}), 400
    if ticker in get_custom_tickers():
        return jsonify({'status': 'error', 'error': f'{ticker} is a custom asset, not a market ticker'}), 400

    try:
        entries = sec_edgar_provider.fetch_shares_history(ticker)
    except sec_edgar_provider.SecThrottled as e:
        return jsonify({'status': 'rate_limited', 'ticker': ticker, 'error': str(e)}), 429
    except Exception as e:
        return jsonify({'status': 'error', 'ticker': ticker, 'error': str(e)}), 502

    if not entries:
        cik = sec_edgar_provider.get_cik(ticker)
        reason = 'no CIK found for ticker (not SEC-registered?)' if not cik else 'no shares-outstanding filings found'
        return jsonify({'status': 'no_data', 'ticker': ticker, 'reason': reason}), 200

    written = record_shares_history_bulk(ticker, entries)
    return jsonify({
        'status': 'success',
        'ticker': ticker,
        'entriesFound': len(entries),
        'entriesStored': written,
    }), 200


@app.route('/update/fundamentals', methods=['POST'])
def update_fundamentals():
    """
    Backfill fundamentals (assets, liabilities, equity, revenue, net/operating
    income, diluted EPS, cash, long-term debt, R&D spend, buyback spend,
    dividend/share) for one US-listed ticker from SEC EDGAR XBRL filings.
    Body: {"ticker": "MSFT"}. Whole-doc replace in companyFundamentals — a
    concept the company stopped/started reporting is reflected exactly as SEC
    has it on each refresh, not merged with a possibly-stale prior fetch.
    """
    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'status': 'error', 'error': 'ticker is required'}), 400
    if ticker in get_custom_tickers():
        return jsonify({'status': 'error', 'error': f'{ticker} is a custom asset, not a market ticker'}), 400

    try:
        concepts = sec_edgar_provider.fetch_fundamentals(ticker)
    except sec_edgar_provider.SecThrottled as e:
        return jsonify({'status': 'rate_limited', 'ticker': ticker, 'error': str(e)}), 429
    except Exception as e:
        return jsonify({'status': 'error', 'ticker': ticker, 'error': str(e)}), 502

    if not concepts:
        cik = sec_edgar_provider.get_cik(ticker)
        reason = 'no CIK found for ticker (not SEC-registered?)' if not cik else 'no fundamentals concepts found'
        return jsonify({'status': 'no_data', 'ticker': ticker, 'reason': reason}), 200

    fundamentals_collection.update_one(
        {'_id': ticker},
        {'$set': {'ticker': ticker, 'concepts': concepts, 'updatedAt': datetime.now()}},
        upsert=True,
    )
    return jsonify({'status': 'success', 'ticker': ticker, 'concepts': list(concepts.keys())}), 200


@app.route('/update/statistics', methods=['POST'])
def update_statistics():
    """
    Refresh only marketData.statistics for one ticker. Body: {"ticker": "MSFT"}.
    One Yahoo request (.info) — no history download — so it's the cheap path for
    an on-demand refresh from the Statistics page. sharesOutstanding (and its
    history) rides along since .info already carries it.
    """
    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').strip()
    if not ticker:
        return jsonify({'status': 'error', 'error': 'ticker is required'}), 400
    if ticker in get_custom_tickers():
        return jsonify({'status': 'skipped', 'reason': 'custom asset ticker', 'ticker': ticker}), 200

    try:
        info = ticker_info(yahoo_crypto_symbol(ticker))
        stats = updateMarketDataUtilities.get_statistics(info, ticker)
    except Exception as e:
        print(f'[statistics] Error fetching {ticker}: {e}')
        return jsonify({'status': 'error', 'ticker': ticker, 'error': str(e)}), 502

    if not stats:
        return jsonify({'status': 'no_data', 'ticker': ticker,
                        'reason': 'provider returned no statistics fields'}), 200

    update = {'statistics': stats, 'ticker': ticker, 'updatedAt': datetime.now()}
    shares = get_shares_with_fallback(ticker, info)
    if shares:
        update['sharesOutstanding'] = shares
        record_shares_history(ticker, shares)
    collection.update_one({'ticker': ticker}, {'$set': update}, upsert=True)
    return jsonify({'status': 'success', 'ticker': ticker, 'fields': len(stats)}), 200


def sec_update_one(ticker: str) -> dict:
    """
    Fundamentals + shares-outstanding history for one ticker from SEC EDGAR.
    Returns {'fundamentals': bool, 'shares': int} — False/0 simply means the
    filer never tagged that data (or has no CIK), which is not an error.
    Lets SecThrottled propagate: the caller must stop the whole run.
    """
    concepts = sec_edgar_provider.fetch_fundamentals(ticker)
    if concepts:
        fundamentals_collection.update_one(
            {'_id': ticker},
            {'$set': {'ticker': ticker, 'concepts': concepts, 'updatedAt': datetime.now()}},
            upsert=True,
        )
    entries = sec_edgar_provider.fetch_shares_history(ticker)
    stored = record_shares_history_bulk(ticker, entries) if entries else 0
    return {'fundamentals': bool(concepts), 'shares': stored}


def run_sec_bulk_task(tickers: list[str], pause_seconds: float):
    """
    Sequential SEC EDGAR backfill over many tickers. Deliberately serial: one
    ticker costs ~15 SEC requests (14 fundamentals concepts + shares history),
    each spaced by sec_edgar_provider.SECONDS_PER_REQUEST (default 5 s, so ~75 s
    per ticker). pause_seconds adds slack between tickers on top of that. A full
    portfolio run takes tens of minutes by design — SEC access is the scarce
    resource here, not wall-clock time.

    A SecThrottled (403/429) aborts the whole run instead of retrying — pushing
    through SEC's throttle is what escalates it to an IP block. Everything
    already written stays; re-run later to continue.
    """
    try:
        with sec_bulk_lock:
            sec_bulk_status.update({
                "total": len(tickers), "processed": 0, "fundamentalsUpdated": 0,
                "sharesUpdated": 0, "noData": [], "failed": [], "currentTicker": None,
                "abortedReason": None, "startedAt": datetime.now().isoformat(), "finishedAt": None,
            })

        print(f"[{datetime.now()}] [sec-bulk] Starting SEC backfill of {len(tickers)} tickers "
              f"({sec_edgar_provider.SECONDS_PER_REQUEST}s between SEC requests, "
              f"{pause_seconds}s between tickers)...")

        for i, ticker in enumerate(tickers, 1):
            if i > 1 and pause_seconds > 0 and not _sleep_unless_cancelled(pause_seconds, sec_bulk_lock, sec_bulk_status):
                break
            if _cancelled(sec_bulk_lock, sec_bulk_status):
                break
            with sec_bulk_lock:
                sec_bulk_status["currentTicker"] = ticker

            try:
                result = sec_update_one(ticker)
            except sec_edgar_provider.SecThrottled as e:
                print(f"[sec-bulk] ABORTED at {ticker}: {e}")
                with sec_bulk_lock:
                    sec_bulk_status["abortedReason"] = str(e)
                return
            except Exception as e:
                print(f"[sec-bulk] [{i}/{len(tickers)}] {ticker} failed: {e}")
                with sec_bulk_lock:
                    sec_bulk_status["failed"].append(ticker)
                    sec_bulk_status["processed"] = i
                continue

            with sec_bulk_lock:
                if result['fundamentals']:
                    sec_bulk_status["fundamentalsUpdated"] += 1
                if result['shares']:
                    sec_bulk_status["sharesUpdated"] += 1
                if not result['fundamentals'] and not result['shares']:
                    sec_bulk_status["noData"].append(ticker)
                sec_bulk_status["processed"] = i

            print(f"[sec-bulk] [{i}/{len(tickers)}] {ticker}: "
                  f"fundamentals={'yes' if result['fundamentals'] else 'no'} "
                  f"sharesPoints={result['shares']}")

        print(f"[{datetime.now()}] [sec-bulk] Task finished.")
    finally:
        with sec_bulk_lock:
            if sec_bulk_status["cancelRequested"]:
                # never clobber a throttle abort — that reason matters more
                if not sec_bulk_status["abortedReason"]:
                    sec_bulk_status["abortedReason"] = "cancelled by user"
                print(f"[{datetime.now()}] [sec-bulk] Cancelled after "
                      f"{sec_bulk_status['processed']}/{sec_bulk_status['total']} tickers.")
            sec_bulk_status["cancelRequested"] = False
            sec_bulk_status["running"] = False
            sec_bulk_status["currentTicker"] = None
            sec_bulk_status["finishedAt"] = datetime.now().isoformat()


@app.route('/update/sec/all', methods=['POST'])
def update_sec_all():
    """
    Background SEC EDGAR backfill (fundamentals + shares-outstanding history)
    for every ticker in marketData, or an explicit {"tickers": [...]} subset.
    Body: {"pauseSeconds": 1, "tickers": [...]}.
    Returns immediately; poll GET /update/sec/all/status.
    """
    data = request.get_json(silent=True) or {}
    try:
        pause_seconds = float(data.get('pauseSeconds', 1))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "pauseSeconds must be a number"}), 400
    if pause_seconds < 0:
        return jsonify({"status": "error", "error": "pauseSeconds must be >= 0"}), 400

    requested = data.get('tickers')
    if requested is not None and not isinstance(requested, list):
        return jsonify({"status": "error", "error": "tickers must be a list"}), 400

    if requested:
        tickers = [str(t).strip().upper() for t in requested if str(t).strip()]
    else:
        tickers = sorted(set(
            doc['ticker'] for doc in collection.find({}, {'ticker': 1}) if doc.get('ticker')
        ))

    # Custom assets hold user-set data and their names can collide with real
    # exchange tickers — they must never hit a provider.
    custom_tickers = get_custom_tickers()
    skipped_custom = [t for t in tickers if t in custom_tickers]
    tickers = [t for t in tickers if t not in custom_tickers]

    if not tickers:
        return jsonify({"status": "error", "error": "no tickers to process",
                        "skippedCustom": skipped_custom}), 400

    with sec_bulk_lock:
        if sec_bulk_status["running"]:
            return jsonify({
                "status": "already_running",
                "processed": sec_bulk_status["processed"],
                "total": sec_bulk_status["total"],
                "currentTicker": sec_bulk_status["currentTicker"],
            }), 409
        sec_bulk_status["running"] = True

    threading.Thread(target=run_sec_bulk_task, args=[tickers, pause_seconds], daemon=True).start()

    # ~15 SEC requests per ticker at the configured spacing, plus the inter-ticker pause
    spacing = sec_edgar_provider.SECONDS_PER_REQUEST
    estimate_seconds = int(len(tickers) * (15 * spacing + pause_seconds))
    return jsonify({
        "status": "started",
        "tickers": len(tickers),
        "skippedCustom": skipped_custom,
        "secondsPerRequest": spacing,
        "pauseSeconds": pause_seconds,
        "estimatedSeconds": estimate_seconds,
        "estimatedMinutes": round(estimate_seconds / 60, 1),
    }), 202


@app.route('/update/sec/all/status', methods=['GET'])
def update_sec_all_status():
    with sec_bulk_lock:
        return jsonify(dict(sec_bulk_status)), 200


@app.route('/update/sec/all/cancel', methods=['POST'])
def update_sec_all_cancel():
    """
    Ask the running SEC backfill to stop. Cooperative: the ticker in flight
    finishes its ~15 SEC requests first (up to ~75 s at the default pacing), then
    the run ends. Everything already written stays; re-run later to continue.
    """
    with sec_bulk_lock:
        if not sec_bulk_status["running"]:
            return jsonify({"status": "not_running"}), 409
        sec_bulk_status["cancelRequested"] = True
        return jsonify({
            "status": "cancelling",
            "processed": sec_bulk_status["processed"],
            "total": sec_bulk_status["total"],
            "currentTicker": sec_bulk_status["currentTicker"],
        }), 202


def fetch_shares_outstanding(ticker: str):
    """Fetch only sharesOutstanding for a ticker from Yahoo Finance (IOB .IL → .L fallback)."""
    return get_shares_with_fallback(ticker, ticker_info(ticker))


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
            record_shares_history(t, shares)
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


def monthly_from_daily(daily_entries: list) -> list:
    """
    Last close of each calendar month from a stored daily series
    ([{date: 'YYYY-MM-DD', price, rawPrice}] ascending). Matches Yahoo's own
    interval='1mo' bars exactly (verified across 500 shared months) and reaches
    further back — Yahoo caps the monthly endpoint at 500 bars while the daily
    series does not. rawPrice rides along when the daily entries carry one
    (pre-rawPrice docs don't; the key is then simply absent).
    """
    by_month: dict[str, dict] = {}
    for entry in daily_entries or []:
        date = entry.get('date')
        price = _finite(entry.get('price'))  # NaN would poison the whole month
        if not date or price is None:
            continue
        point = {'date': str(date)[:7], 'price': round(price, 4)}
        raw = _finite(entry.get('rawPrice'))
        if raw is not None:
            point['rawPrice'] = round(raw, 4)
        by_month[point['date']] = point  # ascending input → last write wins
    return [by_month[m] for m in sorted(by_month)]


def get_monthly_price_history(ticker: str, daily_entries: list | None = None) -> bool:
    """
    Store monthly price history. Derived from an already-stored daily series when
    one is passed (no Yahoo request at all); otherwise downloaded. Returns True on success.
    """
    if daily_entries:
        entries = monthly_from_daily(daily_entries)
        if not entries:
            return False
        price_history_collection.update_one(
            {'_id': ticker},
            {'$set': {'monthlyHistory': entries, 'lastUpdated': datetime.now().strftime('%Y-%m-%d')}},
            upsert=True,
        )
        return True
    try:
        stock = yf.Ticker(yahoo_crypto_symbol(ticker))
        hist = stock.history(period='max', interval='1mo', auto_adjust=False)
        if hist.empty:
            return False
        entries = [{**e, 'date': e['date'][:7]} for e in history_entries(hist)]
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

    # The daily series was already written from the frame that fetch downloaded —
    # re-downloading it here was a straight duplicate. Monthly is folded out of it.
    cached = price_history_collection.find_one({'_id': ticker}, {'history': 1}) or {}
    daily_entries = cached.get('history') or []
    daily_ok = bool(daily_entries)
    monthly_ok = get_monthly_price_history(ticker, daily_entries)

    return jsonify({
        'status': 'success',
        'ticker': ticker,
        'marketData': result['message'],
        'dailyHistory': 'ok' if daily_ok else 'failed',
        'monthlyHistory': 'ok' if monthly_ok else 'failed',
    }), 200


THROTTLED_SCOPES = ('queue', 'holdings', 'all')


def resolve_throttled_tickers(scope: str) -> tuple[list[str], list[str]]:
    """
    Ticker list for a throttled run, by scope:
      queue    — the 'tickers' work queue: only what a transaction enqueued since
                 the last run, and emptied as it is processed (the default, and
                 why a run usually covers a handful of tickers, not the portfolio)
      holdings — every ticker currently held in any portfolio
      all      — every ticker that already has a marketData doc, held or not
    Returns (tickers, skipped_custom). Custom assets are never sent to a provider.
    """
    if scope == 'holdings':
        raw = holdings_collection.distinct('ticker')
    elif scope == 'all':
        raw = collection.distinct('ticker')
    else:
        raw = tickers_collection.distinct('ticker')

    custom_tickers = get_custom_tickers()
    skipped_custom = sorted({str(t) for t in raw if t and str(t) in custom_tickers})
    tickers = sorted({str(t) for t in raw if t and str(t) not in custom_tickers})

    # A custom ticker sitting in the queue would be retried forever — drop it.
    if scope == 'queue' and skipped_custom:
        tickers_collection.delete_many({'ticker': {'$in': skipped_custom}})
        print(f"[throttled] Skipped {len(skipped_custom)} custom asset tickers: {skipped_custom}")

    return tickers, skipped_custom


def _record_throttled_failure(ticker: str, stage: str, error: str, position: str) -> None:
    """
    Book one failed ticker with the reason and the stage it died at ('fetch' =
    the provider never returned data, 'db' = it did and the write failed), so the
    status answers *why* without a trip to the container logs.
    """
    with throttled_lock:
        throttled_status["failed"].append({
            "ticker": ticker,
            "stage": stage,
            "error": error,
            "at": datetime.now().isoformat(),
        })
        throttled_status["lastError"] = f"{ticker}: {error}"
    throttled_event('err', f"[{position}] {ticker} failed ({stage}): {error}", ticker)


def run_throttled_task(ticker_symbols: list[str], pause_seconds: float, scope: str = 'queue'):
    """
    Sequential Yahoo update of the given tickers, sleeping between them to stay
    under rate limits. Each ticker is written to marketData and removed from the
    queue immediately, so an interrupted run loses nothing.

    Pause and cancel are both checked between tickers only: the ticker in flight
    is always finished and written first.
    """
    try:
        total = len(ticker_symbols)
        with throttled_lock:
            throttled_status.update({
                "scope": scope,
                "total": total, "processed": 0, "updated": 0, "failed": [],
                "pauseSeconds": pause_seconds,
                "currentTicker": None, "currentStage": None, "lastError": None,
                "paused": False, "pausedAt": None, "pausedSeconds": 0.0,
                "abortedReason": None,
                "startedAt": datetime.now().isoformat(), "finishedAt": None,
                # Feed cleared per run, but eventSeq keeps counting for the life of
                # the process: a poller's cursor must never go backwards under it.
                "events": [],
            })

        throttled_event('info', f"Starting sequential update of {total} tickers "
                                f"[scope={scope}] ({pause_seconds}s pause between every Yahoo request)")

        # pause threaded through the fetch chain: info -> combined history download
        # (dividends + splits + daily prices in one request), plus between tickers below
        def throttled_yahoo_fetch(t: str) -> dict:
            return yahoo_fetch_market_data(t, request_pause=pause_seconds)

        for i, ticker in enumerate(ticker_symbols, 1):
            if not _wait_while_paused():
                break
            if i > 1:
                with throttled_lock:
                    throttled_status["currentTicker"] = ticker
                    throttled_status["currentStage"] = "waiting"
                if not _sleep_unless_cancelled(pause_seconds, throttled_lock, throttled_status):
                    break
                # A pause landing during the inter-ticker sleep parks here, not
                # mid-fetch — the whole point of pausing between tickers.
                if not _wait_while_paused():
                    break
            if _cancelled(throttled_lock, throttled_status):
                break

            position = f"{i}/{total}"
            with throttled_lock:
                throttled_status["currentTicker"] = ticker
                throttled_status["currentStage"] = "fetching"
            throttled_event('info', f"[{position}] {ticker}: fetching from Yahoo", ticker)

            processed_ticker, operation, error = process_ticker(
                ticker, throttled_yahoo_fetch, request_pause=pause_seconds)

            if operation:
                with throttled_lock:
                    throttled_status["currentStage"] = "saving"
                try:
                    collection.bulk_write([operation])
                    tickers_collection.delete_many({'ticker': processed_ticker})
                    with throttled_lock:
                        throttled_status["updated"] += 1
                    throttled_event('ok', f"[{position}] {processed_ticker}: updated", processed_ticker)
                except Exception as e:
                    _record_throttled_failure(processed_ticker, 'db', str(e), position)
            else:
                _record_throttled_failure(processed_ticker, 'fetch', str(error), position)

            with throttled_lock:
                throttled_status["processed"] = i
                throttled_status["currentStage"] = None
    except Exception as e:
        # A worker that dies silently looks identical to one that finished: record
        # the reason where the status endpoint shows it, and the trace on stderr.
        traceback.print_exc()
        with throttled_lock:
            throttled_status["abortedReason"] = f"crashed: {e}"
            throttled_status["lastError"] = str(e)
        throttled_event('err', f"Run crashed: {e}")
    finally:
        with throttled_lock:
            if throttled_status["cancelRequested"]:
                throttled_status["abortedReason"] = "cancelled by user"
            processed, total_final = throttled_status["processed"], throttled_status["total"]
            aborted = throttled_status["abortedReason"]
            failed_count = len(throttled_status["failed"])
            updated_count = throttled_status["updated"]
            throttled_status["cancelRequested"] = False
            throttled_status["paused"] = False
            throttled_status["pausedAt"] = None
            throttled_status["currentTicker"] = None
            throttled_status["currentStage"] = None
            throttled_status["running"] = False
            throttled_status["finishedAt"] = datetime.now().isoformat()
        summary = (f"Finished {processed}/{total_final} — {updated_count} updated, "
                   f"{failed_count} failed" + (f" — aborted: {aborted}" if aborted else ""))
        throttled_event('warn' if aborted or failed_count else 'ok', summary)


@app.route('/update/throttled', methods=['POST'])
def update_throttled():
    """
    Start a background sequential Yahoo update, pausing between calls (default
    15 s, override via {"pauseSeconds": N}). {"scope": "queue"|"holdings"|"all"}
    picks the ticker list — see resolve_throttled_tickers. Returns immediately
    with the resolved count; poll GET /update/throttled/status for progress.
    """
    data = request.get_json(silent=True) or {}
    try:
        pause_seconds = float(data.get('pauseSeconds', 15))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "pauseSeconds must be a number"}), 400
    if pause_seconds < 0:
        return jsonify({"status": "error", "error": "pauseSeconds must be >= 0"}), 400

    scope = str(data.get('scope') or 'queue').lower()
    if scope not in THROTTLED_SCOPES:
        return jsonify({"status": "error",
                        "error": f"scope must be one of {', '.join(THROTTLED_SCOPES)}"}), 400

    # Resolved here, not in the worker, so the caller gets the real count up front.
    tickers, skipped_custom = resolve_throttled_tickers(scope)
    if not tickers:
        return jsonify({"status": "error", "error": "no tickers to process",
                        "scope": scope, "skippedCustom": len(skipped_custom)}), 400

    with throttled_lock:
        if throttled_status["running"]:
            return jsonify({
                "status": "already_running",
                "scope": throttled_status["scope"],
                "processed": throttled_status["processed"],
                "total": throttled_status["total"],
            }), 409
        throttled_status["running"] = True

    threading.Thread(target=run_throttled_task, args=[tickers, pause_seconds, scope], daemon=True).start()

    # Two pauses per ticker: one inside the fetch (.info -> history), one between tickers.
    estimate_seconds = int((2 * len(tickers) - 1) * pause_seconds)
    return jsonify({
        "status": "started",
        "scope": scope,
        "tickers": len(tickers),
        "skippedCustom": len(skipped_custom),
        "pauseSeconds": pause_seconds,
        "estimatedSeconds": estimate_seconds,
        "estimatedMinutes": round(estimate_seconds / 60, 1),
        "message": f"Throttled Yahoo batch started for {len(tickers)} tickers "
                   f"(scope={scope}, {pause_seconds}s pause between every Yahoo request)",
    }), 202


@app.route('/update/throttled/status', methods=['GET'])
def update_throttled_status():
    """
    Progress of the current/last run. `?sinceSeq=N` returns only events newer than
    seq N (a poller's cursor) — omit it for everything still buffered. `eventSeq`
    is the newest sequence number, i.e. the cursor to send next time.
    """
    try:
        since_seq = int(request.args.get('sinceSeq', 0))
    except (TypeError, ValueError):
        since_seq = 0

    with throttled_lock:
        snapshot = dict(throttled_status)
        events = [e for e in snapshot["events"] if e["seq"] > since_seq]

    snapshot["events"] = events
    # Pauses stretch wall-clock time, so the ETA counts only tickers not yet done
    # and assumes the run is resumed now. Two pauses per ticker, as in the estimate
    # returned at start.
    pause = snapshot.get("pauseSeconds")
    remaining = max(snapshot["total"] - snapshot["processed"], 0)
    snapshot["remainingTickers"] = remaining
    snapshot["remainingSeconds"] = int(remaining * 2 * pause) if pause is not None else None
    # The worker only books pause time when it resumes, so a hold in progress would
    # otherwise read 0 for as long as it lasts — add the current one live.
    paused_seconds = snapshot.get("pausedSeconds") or 0.0
    if snapshot.get("paused") and snapshot.get("pausedAt"):
        try:
            paused_seconds += (datetime.now() - datetime.fromisoformat(snapshot["pausedAt"])).total_seconds()
        except (TypeError, ValueError):
            pass
    snapshot["pausedSeconds"] = round(paused_seconds, 1)
    return jsonify(snapshot), 200


@app.route('/update/throttled/pause', methods=['POST'])
def update_throttled_pause():
    """
    Hold the running batch after the ticker in flight. The run keeps its slot
    (status stays running=true, paused=true) so nothing else can start over it,
    and nothing is lost — resume continues with the next ticker in the same list.
    """
    with throttled_lock:
        if not throttled_status["running"]:
            return jsonify({"status": "not_running"}), 409
        if throttled_status["cancelRequested"]:
            return jsonify({"status": "cancelling"}), 409
        if throttled_status["paused"]:
            return jsonify({"status": "already_paused",
                            "processed": throttled_status["processed"],
                            "total": throttled_status["total"]}), 409
        throttled_status["paused"] = True
        throttled_status["pausedAt"] = datetime.now().isoformat()
        processed, total = throttled_status["processed"], throttled_status["total"]

    throttled_event('warn', f"Pause requested at {processed}/{total} — holding after the current ticker")
    return jsonify({"status": "pausing", "processed": processed, "total": total}), 202


@app.route('/update/throttled/resume', methods=['POST'])
def update_throttled_resume():
    """Let a paused batch carry on from where it stopped."""
    with throttled_lock:
        if not throttled_status["running"]:
            return jsonify({"status": "not_running"}), 409
        if not throttled_status["paused"]:
            return jsonify({"status": "not_paused"}), 409
        throttled_status["paused"] = False
        throttled_status["pausedAt"] = None
        throttled_status["currentStage"] = None
        processed, total = throttled_status["processed"], throttled_status["total"]

    throttled_event('info', f"Resumed at {processed}/{total}")
    return jsonify({"status": "resumed", "processed": processed, "total": total}), 202


@app.route('/update/throttled/cancel', methods=['POST'])
def update_throttled_cancel():
    """
    Ask the running throttled batch to stop. Cooperative: the ticker in flight is
    finished and written, then the run ends — nothing is rolled back, and the
    queue keeps whatever was not reached, so a later run continues from there.
    A paused run stops too: cancel beats pause, it does not wait for a resume.
    """
    with throttled_lock:
        if not throttled_status["running"]:
            return jsonify({"status": "not_running"}), 409
        throttled_status["cancelRequested"] = True
        processed, total = throttled_status["processed"], throttled_status["total"]

    throttled_event('warn', f"Stop requested at {processed}/{total} — ending after the current ticker")
    return jsonify({
        "status": "cancelling",
        "processed": processed,
        "total": total,
    }), 202


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
        '/update/fundamentals': {
            'post': {
                'summary': 'Backfill fundamentals (assets/liabilities/equity/revenue/income/EPS/cash/debt/R&D/buybacks/dividend-per-share) for one US-listed ticker from SEC EDGAR',
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['ticker'],
                        'properties': {'ticker': {'type': 'string', 'example': 'MSFT'}},
                    }}},
                },
                'responses': {'200': {'description': '{status, ticker, concepts: [keys]} or {status: "no_data", reason}'},
                              '400': {'description': 'ticker missing or is a custom asset'},
                              '429': {'description': 'SEC rate-limited/blocked this client (403/429) — back off, do not retry immediately'},
                              '502': {'description': 'SEC EDGAR request failed'}},
            },
        },
        '/update/statistics': {
            'post': {
                'summary': 'Refresh marketData.statistics (key statistics: margins, valuation, balance sheet, dividends, analyst view) for one ticker — one Yahoo .info request, no history download',
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['ticker'],
                        'properties': {'ticker': {'type': 'string', 'example': 'MSFT'}},
                    }}},
                },
                'responses': {'200': {'description': '{status, ticker, fields} or {status: "no_data"|"skipped", reason}'},
                              '400': {'description': 'ticker required'},
                              '502': {'description': 'Yahoo request failed'}},
            },
        },
        '/update/sharesOutstandingHistory': {
            'post': {
                'summary': 'Backfill historical sharesOutstanding for one US-listed ticker from SEC EDGAR XBRL filings (does not touch the live marketData value)',
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['ticker'],
                        'properties': {'ticker': {'type': 'string', 'example': 'MSFT'}},
                    }}},
                },
                'responses': {'200': {'description': '{status, ticker, entriesFound, entriesStored} or {status: "no_data", reason}'},
                              '400': {'description': 'ticker missing or is a custom asset'},
                              '429': {'description': 'SEC rate-limited/blocked this client (403/429) — back off, do not retry immediately'},
                              '502': {'description': 'SEC EDGAR request failed'}},
            },
        },
        '/update/sec/all': {
            'post': {
                'summary': 'Bulk SEC EDGAR backfill (fundamentals + shares-outstanding history) for every marketData ticker, or a given subset',
                'description': (
                    'Runs in the background, one ticker at a time. Each ticker costs ~15 SEC requests '
                    '(14 fundamentals concepts + shares history), and every SEC request is spaced '
                    'process-wide by SEC_SECONDS_PER_REQUEST — default 5 s, i.e. ~75 s per ticker, so a '
                    'whole portfolio takes tens of minutes. That is well under SEC\'s 10 req/s fair-access '
                    'ceiling on purpose. pauseSeconds adds slack between tickers on top. '
                    'A 403/429 from SEC aborts the run (abortedReason in status) rather than retrying, '
                    'because pushing through a throttle escalates it to an IP block. '
                    'Custom-asset tickers are skipped; non-SEC-registered tickers (foreign issuers, crypto) '
                    'land in noData, which is not an error. Poll GET /update/sec/all/status for progress.'
                ),
                'requestBody': {
                    'required': False,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'properties': {
                            'pauseSeconds': {'type': 'number', 'example': 1,
                                             'description': 'Extra pause between tickers (default 1)'},
                            'tickers': {'type': 'array', 'items': {'type': 'string'},
                                        'example': ['MSFT', 'KO'],
                                        'description': 'Subset to process; omit for every marketData ticker'},
                        },
                    }}},
                },
                'responses': {
                    '202': {'description': '{status: "started", tickers, skippedCustom[], secondsPerRequest, pauseSeconds, estimatedSeconds, estimatedMinutes}'},
                    '400': {'description': 'Invalid pauseSeconds/tickers, or nothing to process'},
                    '409': {'description': 'A SEC bulk run is already in progress'},
                },
            },
        },
        '/update/sec/all/status': {
            'get': {
                'summary': 'Progress of the current/last SEC bulk backfill',
                'responses': {'200': {'description': '{running, total, processed, fundamentalsUpdated, sharesUpdated, noData[], failed[], currentTicker, cancelRequested, abortedReason, startedAt, finishedAt}'}},
            },
        },
        '/update/sec/all/cancel': {
            'post': {
                'summary': 'Stop the running SEC bulk backfill',
                'description': (
                    'Cooperative stop. The ticker in flight finishes its ~15 SEC requests first '
                    '(up to ~75 s at default pacing), then the run ends with abortedReason '
                    '"cancelled by user". Work already written stays; re-run later to continue.'
                ),
                'responses': {'202': {'description': '{status: "cancelling", processed, total, currentTicker}'},
                              '409': {'description': 'No SEC bulk run is in progress'}},
            },
        },
        '/update/throttled': {
            'post': {
                'summary': 'Background sequential Yahoo update, pausing between every Yahoo request (default 15 s)',
                'description': (
                    'scope picks the ticker list: "queue" (default) takes the tickers work queue — '
                    'only what a transaction enqueued since the last run, emptied as it is processed; '
                    '"holdings" takes every ticker currently held in any portfolio; "all" takes every '
                    'ticker that has a marketData doc. Custom assets are always skipped. '
                    'Roughly 4 Yahoo requests and 2 pauses per ticker.'
                ),
                'requestBody': {
                    'required': False,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'properties': {
                            'pauseSeconds': {'type': 'number', 'example': 15},
                            'scope': {'type': 'string', 'enum': list(THROTTLED_SCOPES),
                                      'example': 'holdings'},
                        },
                    }}},
                },
                'responses': {'202': {'description': '{status: "started", scope, tickers, skippedCustom, pauseSeconds, estimatedSeconds, estimatedMinutes}'},
                              '400': {'description': 'Invalid pauseSeconds/scope, or nothing to process'},
                              '409': {'description': 'A throttled batch is already running'}},
            },
        },
        '/update/throttled/status': {
            'get': {
                'summary': 'Progress of the current/last throttled batch',
                'description': (
                    'failed[] entries are {ticker, stage, error, at} — stage "fetch" means the '
                    'provider never returned data, "db" means the write failed. events[] is the '
                    'live per-ticker feed; pass sinceSeq to get only what is new and use eventSeq '
                    'as the next cursor.'
                ),
                'parameters': [{
                    'name': 'sinceSeq', 'in': 'query', 'required': False,
                    'schema': {'type': 'integer'}, 'example': 0,
                    'description': 'Return only events with seq greater than this',
                }],
                'responses': {'200': {'description': '{running, paused, pausedAt, pausedSeconds, scope, total, processed, updated, failed[], pauseSeconds, currentTicker, currentStage, lastError, remainingTickers, remainingSeconds, cancelRequested, abortedReason, startedAt, finishedAt, events[], eventSeq}'}},
            },
        },
        '/update/throttled/pause': {
            'post': {
                'summary': 'Pause the running throttled batch',
                'description': (
                    'Cooperative hold. The ticker in flight is finished and written, then the '
                    'worker parks between tickers. The run keeps its slot (running stays true, '
                    'paused becomes true), so nothing else can start over it; POST '
                    '/update/throttled/resume carries on with the next ticker in the same list.'
                ),
                'responses': {'202': {'description': '{status: "pausing", processed, total}'},
                              '409': {'description': 'Not running, already paused, or already cancelling'}},
            },
        },
        '/update/throttled/resume': {
            'post': {
                'summary': 'Resume a paused throttled batch',
                'responses': {'202': {'description': '{status: "resumed", processed, total}'},
                              '409': {'description': 'No run in progress, or it is not paused'}},
            },
        },
        '/update/throttled/cancel': {
            'post': {
                'summary': 'Stop the running throttled batch',
                'description': (
                    'Cooperative stop. The ticker in flight is finished and written, then the run '
                    'ends with abortedReason "cancelled by user". Tickers not reached stay in the '
                    'queue, so a later run continues from there. Works on a paused run too — '
                    'cancel beats pause and does not wait for a resume.'
                ),
                'responses': {'202': {'description': '{status: "cancelling", processed, total}'},
                              '409': {'description': 'No throttled batch is in progress'}},
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


# ---------- Admin panel (one page, triggers the update endpoints) ----------

ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Data Admin</title>
<style>
  :root {
    --bg: #0F172A; --panel: #1E293B; --panel2: #172033; --line: #334155;
    --text: #E2E8F0; --muted: #94A3B8; --subtle: #64748B;
    --accent: #6366F1; --ok: #10B981; --warn: #F59E0B; --err: #EF4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }
  h1 { font-size: 20px; margin: 0; }
  .sub { color: var(--muted); font-size: 12.5px; margin-top: 4px; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .banner {
    margin: 16px 0 20px; padding: 10px 12px; border-radius: 8px; font-size: 12.5px;
    background: rgba(245,158,11,.12); border: 1px solid rgba(245,158,11,.35); color: #FCD34D;
  }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
  .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
  .stat .k { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--subtle); }
  .stat .v { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; margin: 0 0 2px; }
  .card p.hint { color: var(--muted); font-size: 12px; margin: 0 0 14px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .row + .row { margin-top: 10px; }
  label.f { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
  input[type=text], input[type=number] {
    background: var(--panel2); border: 1px solid var(--line); color: var(--text);
    border-radius: 6px; padding: 7px 9px; font: inherit; font-size: 13px;
  }
  input[type=text] { text-transform: uppercase; }
  input#secTickers { text-transform: uppercase; min-width: 260px; }
  input[type=number] { width: 76px; }
  button {
    background: var(--panel2); border: 1px solid var(--line); color: var(--text);
    border-radius: 6px; padding: 7px 12px; font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { filter: brightness(1.1); }
  button.danger { border-color: rgba(239,68,68,.5); color: #FCA5A5; }
  button.danger:hover:not(:disabled) { background: rgba(239,68,68,.15); border-color: var(--err); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .seg { display: inline-flex; background: var(--panel2); border: 1px solid var(--line); border-radius: 6px; padding: 2px; }
  .seg button { background: transparent; border: none; padding: 5px 10px; font-size: 12.5px; font-weight: 600; color: var(--muted); }
  .seg button.on { background: var(--accent); color: #fff; border-radius: 4px; }
  .seg button b { font-variant-numeric: tabular-nums; opacity: .7; font-weight: 700; margin-left: 3px; }
  .bar { height: 8px; border-radius: 4px; background: var(--panel2); overflow: hidden; margin-top: 10px; }
  .bar > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .3s; }
  .prog { font-size: 12.5px; color: var(--muted); margin-top: 8px; font-variant-numeric: tabular-nums; }
  .prog b { color: var(--text); }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--subtle); margin-right: 6px; }
  .dot.run { background: var(--ok); animation: pulse 1.2s infinite; }
  .dot.err { background: var(--err); }
  @keyframes pulse { 50% { opacity: .3; } }
  #log { max-height: 320px; overflow: auto; font: 12px/1.7 ui-monospace, SFMono-Regular, Consolas, monospace; }
  #log div { border-bottom: 1px solid rgba(51,65,85,.5); padding: 3px 0; white-space: pre-wrap; word-break: break-word; }
  #log .t { color: var(--subtle); margin-right: 8px; }
  .ok { color: var(--ok); } .warn { color: var(--warn); } .err { color: var(--err); }
  a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Market Data Admin</h1>
      <div class="sub">portfolio-scripts-backend &middot; triggers the /update endpoints &middot; <a href="/apidocs">API docs</a></div>
    </div>
    <button onclick="loadSummary()">Refresh stats</button>
  </header>

  <div class="banner">
    No authentication on this service. Keep port 5000 bound to loopback &mdash; anyone who can reach it can run these jobs.
  </div>

  <div class="stats" id="stats"></div>

  <div class="card">
    <h2>Price update &mdash; one ticker</h2>
    <p class="hint">auto routes crypto to CoinGecko and everything else to Yahoo. full also refreshes daily + monthly price history.</p>
    <div class="row">
      <input type="text" id="priceTicker" placeholder="AAPL" size="12">
      <span class="seg" id="providerSeg">
        <button class="on" data-p="auto">auto</button>
        <button data-p="yahoo">yahoo</button>
        <button data-p="crypto">crypto</button>
        <button data-p="massive">massive</button>
        <button data-p="full">full</button>
      </span>
      <button class="primary" onclick="updateOne()">Update ticker</button>
      <button onclick="refreshDaily()">Daily history</button>
    </div>
    <div class="row">
      <button onclick="queueBatch()">Queue batch for whole ticker queue (debounced 10 s)</button>
    </div>
  </div>

  <div class="card">
    <h2>Throttled run</h2>
    <p class="hint">Sequential Yahoo pass, sleeping between every request. One run at a time.
      <b>Queue</b> = only what a transaction enqueued since the last run (it empties as it goes).
      <b>Holdings</b> = every ticker you currently hold. <b>All</b> = every ticker with a marketData doc.
      Custom assets are always skipped. Pause holds the run after the ticker in flight and keeps its
      place; every ticker and every failure reason streams into the log below.</p>
    <div class="row">
      <span class="seg" id="scopeSeg">
        <button class="on" data-s="queue">Queue <b id="cntQueue">–</b></button>
        <button data-s="holdings">Holdings <b id="cntHoldings">–</b></button>
        <button data-s="all">All <b id="cntAll">–</b></button>
      </span>
      <label class="f">pause <input type="number" id="thrPause" value="15" min="0" step="1"> s</label>
      <button class="primary" id="thrStart" onclick="startThrottled()">Start run</button>
      <button id="thrHold" onclick="holdThrottled()" disabled>Pause</button>
      <button class="danger" id="thrStop" onclick="stopThrottled()" disabled>Stop</button>
    </div>
    <div class="bar"><i id="thrBar"></i></div>
    <div class="prog" id="thrProg"><span class="dot"></span>idle</div>
  </div>

  <div class="card">
    <h2>FX rates</h2>
    <p class="hint">ECB reference rates into exchangeRates. The frontend converts every displayed total with these.</p>
    <div class="row"><button class="primary" onclick="updateFx()">Refresh ECB rates</button></div>
  </div>

  <div class="card">
    <h2>Statistics &amp; shares</h2>
    <p class="hint">Per ticker. Shares outstanding with an empty ticker backfills every marketData doc.</p>
    <div class="row">
      <input type="text" id="statsTicker" placeholder="MSFT" size="12">
      <button onclick="oneTicker('/update/statistics', 'statistics')">Yahoo statistics</button>
      <button onclick="sharesOutstanding()">Shares outstanding</button>
      <button onclick="oneTicker('/update/sharesOutstandingHistory', 'shares history (SEC)')">Shares history (SEC)</button>
      <button onclick="oneTicker('/update/fundamentals', 'fundamentals (SEC)')">Fundamentals (SEC)</button>
    </div>
  </div>

  <div class="card">
    <h2>SEC bulk backfill</h2>
    <p class="hint">Fundamentals + shares history for every marketData ticker, or the subset listed below. Slow &mdash; roughly 75 s per ticker at the default SEC pacing; aborts rather than pushing through a 429.</p>
    <div class="row">
      <label class="f">pause <input type="number" id="secPause" value="1" min="0" step="0.5"> s</label>
      <input type="text" id="secTickers" placeholder="optional subset: MSFT, AAPL, MA">
      <button class="primary" id="secStart" onclick="startSec()">Start backfill</button>
      <button class="danger" id="secStop" onclick="stopSec()" disabled>Stop</button>
    </div>
    <div class="bar"><i id="secBar"></i></div>
    <div class="prog" id="secProg"><span class="dot"></span>idle</div>
  </div>

  <div class="card">
    <h2>Log</h2>
    <div id="log"></div>
  </div>
</div>

<script>
var provider = 'auto';
document.getElementById('providerSeg').addEventListener('click', function (e) {
  var b = e.target.closest('button');
  if (!b) return;
  provider = b.dataset.p;
  [].forEach.call(this.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === b); });
});

var scope = 'queue';
document.getElementById('scopeSeg').addEventListener('click', function (e) {
  var b = e.target.closest('button');
  if (!b) return;
  scope = b.dataset.s;
  [].forEach.call(this.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === b); });
});

function log(msg, kind) {
  var el = document.getElementById('log');
  var d = document.createElement('div');
  var t = new Date().toTimeString().slice(0, 8);
  d.innerHTML = '<span class="t">' + t + '</span><span class="' + (kind || '') + '">' + msg + '</span>';
  el.insertBefore(d, el.firstChild);
  while (el.childNodes.length > 200) el.removeChild(el.lastChild);
}

function tick(id, required) {
  var v = (document.getElementById(id).value || '').trim().toUpperCase();
  if (!v && required) throw new Error('ticker is required');
  return v;
}

// Every trigger goes through here so each call lands in the log with its status.
function post(path, body, label) {
  log(label + ' \\u2192 ' + path + (body && body.ticker ? ' [' + body.ticker + ']' : ''));
  return fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  }).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      var kind = r.ok ? (j.status === 'error' ? 'err' : (j.status === 'no_data' || j.status === 'skipped' ? 'warn' : 'ok')) : 'err';
      log(label + ' \\u2190 ' + r.status + ' ' + JSON.stringify(j), kind);
      return {res: r, json: j};
    });
  }).catch(function (e) {
    log(label + ' failed: ' + e.message, 'err');
    throw e;
  });
}

function guard(fn) { try { fn(); } catch (e) { log(e.message, 'err'); } }

// Ticker-scoped endpoints: a missing ticker is a logged error, not a silent no-op.
function oneTicker(path, label) {
  guard(function () { post(path, {ticker: tick('statsTicker', true)}, label); });
}

function updateOne() {
  guard(function () {
    var t = tick('priceTicker', true);
    var path = provider === 'full' ? '/update/full' : '/update/' + provider;
    post(path, {ticker: t}, 'price ' + provider).then(loadSummary);
  });
}

function refreshDaily() {
  guard(function () {
    var t = tick('priceTicker', true);
    post('/history/refresh/' + encodeURIComponent(t), {}, 'daily history');
  });
}

function queueBatch() {
  if (provider === 'full') { log('full has no batch mode \\u2014 pick auto/yahoo/crypto/massive', 'warn'); return; }
  if (!confirm('Schedule a ' + provider + ' batch over every ticker in the queue?')) return;
  post('/update/' + provider, {}, 'batch ' + provider);
}

function sharesOutstanding() {
  var t = (document.getElementById('statsTicker').value || '').trim().toUpperCase();
  if (!t && !confirm('No ticker given \\u2014 backfill sharesOutstanding for EVERY marketData doc?')) return;
  post('/update/sharesOutstanding', t ? {ticker: t} : {}, 'sharesOutstanding' + (t ? '' : ' (all)'));
}

function updateFx() {
  post('/update/exchangeRates', {}, 'FX rates').then(loadSummary);
}

function startThrottled() {
  var pause = Number(document.getElementById('thrPause').value);
  var counts = {queue: 'cntQueue', holdings: 'cntHoldings', all: 'cntAll'};
  var n = Number(document.getElementById(counts[scope]).textContent) || 0;
  var mins = Math.round((2 * n - 1) * pause / 60);
  if (!confirm('Start a throttled Yahoo run over ' + n + ' ticker(s) [' + scope + '] at ' + pause + 's per request?\\n\\nRoughly ' + mins + ' min.')) return;
  post('/update/throttled', {pauseSeconds: pause, scope: scope}, 'throttled run [' + scope + ']').then(function (r) {
    if (r.json && r.json.estimatedMinutes != null) log('throttled estimate: ~' + r.json.estimatedMinutes + ' min for ' + r.json.tickers + ' tickers', 'warn');
    pollThrottled();
  });
}

function stopThrottled() {
  document.getElementById('thrStop').disabled = true;
  post('/update/throttled/cancel', {}, 'throttled cancel').then(function () { pollThrottled(); });
}

// One button, both ways: the poll rewrites its label from the run's paused flag.
function holdThrottled() {
  var btn = document.getElementById('thrHold');
  var resume = btn.getAttribute('data-paused') === '1';
  btn.disabled = true;
  post('/update/throttled/' + (resume ? 'resume' : 'pause'), {}, 'throttled ' + (resume ? 'resume' : 'pause'))
    .then(function () { pollThrottled(); })
    .catch(function () { pollThrottled(); });
}

function stopSec() {
  document.getElementById('secStop').disabled = true;
  log('SEC cancel: the ticker in flight finishes first (up to ~75 s)', 'warn');
  post('/update/sec/all/cancel', {}, 'SEC cancel').then(function () { pollSec(); });
}

function startSec() {
  var pause = Number(document.getElementById('secPause').value);
  var raw = (document.getElementById('secTickers').value || '').trim();
  var body = {pauseSeconds: pause};
  if (raw) body.tickers = raw.split(',').map(function (s) { return s.trim().toUpperCase(); }).filter(Boolean);
  if (!confirm('Start SEC backfill' + (body.tickers ? ' for ' + body.tickers.length + ' ticker(s)' : ' for every marketData ticker') + '? This can run for hours.')) return;
  post('/update/sec/all', body, 'SEC bulk').then(function (r) {
    if (r.json && r.json.estimatedMinutes != null) log('SEC bulk estimate: ~' + r.json.estimatedMinutes + ' min for ' + r.json.tickers + ' tickers', 'warn');
    pollSec();
  });
}

function renderProgress(barId, progId, s, extra) {
  var pct = s.total ? Math.round((s.processed / s.total) * 100) : 0;
  document.getElementById(barId).style.width = pct + '%';
  var dot = s.running ? (s.paused ? 'dot' : 'dot run') : (s.abortedReason ? 'dot err' : 'dot');
  var txt;
  if (s.running) txt = '<b>' + s.processed + '/' + s.total + '</b> (' + pct + '%)';
  else if (s.finishedAt) txt = 'finished <b>' + s.processed + '/' + s.total + '</b>';
  else txt = 'idle';
  if (s.currentTicker) txt += ' &middot; ' + s.currentTicker + (s.currentStage ? ' (' + s.currentStage + ')' : '');
  if (extra) txt += extra;
  if (s.paused) txt += ' &middot; <span class="warn">paused</span>';
  if (s.cancelRequested) txt += ' &middot; <span class="warn">stopping after current ticker\\u2026</span>';
  if (s.failed && s.failed.length) txt += ' &middot; <span class="err">' + s.failed.length + ' failed</span>';
  if (s.abortedReason) txt += ' &middot; <span class="err">aborted: ' + s.abortedReason + '</span>';
  document.getElementById(progId).innerHTML = '<span class="' + dot + '"></span>' + txt;
}

var thrTimer = null, secTimer = null;
// Cursor into the run's event feed — only what is new since the last poll is logged.
var thrSeq = 0;

function pollThrottled() {
  fetch('/update/throttled/status?sinceSeq=' + thrSeq).then(function (r) { return r.json(); }).then(function (s) {
    // Service restarted under us — seqs began again, so rewind the cursor.
    if (s.eventSeq != null && s.eventSeq < thrSeq) thrSeq = 0;
    (s.events || []).forEach(function (e) {
      log('throttled: ' + e.message, e.level === 'info' ? '' : e.level);
      if (e.seq > thrSeq) thrSeq = e.seq;
    });
    var thrExtra = (s.scope ? ' &middot; scope ' + s.scope : '') + (s.updated ? ' &middot; ' + s.updated + ' updated' : '');
    if (s.running && !s.paused && s.remainingSeconds) thrExtra += ' &middot; ~' + Math.round(s.remainingSeconds / 60) + ' min left';
    if (s.paused && s.pausedSeconds) thrExtra += ' &middot; held ' + Math.round(s.pausedSeconds) + ' s';
    renderProgress('thrBar', 'thrProg', s, thrExtra);
    document.getElementById('thrStart').disabled = !!s.running;
    // Stop stays live only while a run is going and no cancel is already pending.
    document.getElementById('thrStop').disabled = !s.running || !!s.cancelRequested;
    var hold = document.getElementById('thrHold');
    hold.disabled = !s.running || !!s.cancelRequested;
    hold.setAttribute('data-paused', s.paused ? '1' : '0');
    hold.textContent = s.paused ? 'Resume' : 'Pause';
    clearTimeout(thrTimer);
    // Paused still polls: the label, the ETA and a Stop landing meanwhile all need it.
    if (s.running) thrTimer = setTimeout(pollThrottled, 2000);
    else loadSummary();
  }).catch(function () { clearTimeout(thrTimer); });
}

function pollSec() {
  fetch('/update/sec/all/status').then(function (r) { return r.json(); }).then(function (s) {
    var extra = '';
    if (s.fundamentalsUpdated || s.sharesUpdated) extra = ' &middot; ' + s.fundamentalsUpdated + ' fundamentals, ' + s.sharesUpdated + ' shares';
    if (s.noData && s.noData.length) extra += ' &middot; <span class="warn">' + s.noData.length + ' no data</span>';
    renderProgress('secBar', 'secProg', s, extra);
    document.getElementById('secStart').disabled = !!s.running;
    document.getElementById('secStop').disabled = !s.running || !!s.cancelRequested;
    clearTimeout(secTimer);
    // SEC runs are hours long — a slower poll is plenty and keeps the log quiet.
    if (s.running) secTimer = setTimeout(pollSec, 5000);
    else loadSummary();
  }).catch(function () { clearTimeout(secTimer); });
}

function loadSummary() {
  fetch('/admin/summary').then(function (r) { return r.json(); }).then(function (s) {
    var cells = [
      ['queue', s.queue], ['market data', s.marketData], ['custom (skipped)', s.customAssets],
      ['fx rates', s.fxRates], ['price history', s.priceHistory],
      ['fundamentals', s.fundamentals], ['shares history', s.sharesHistory],
      ['last update', s.lastUpdatedAt ? s.lastUpdatedAt.replace('T', ' ').slice(0, 16) : '\\u2014']
    ];
    document.getElementById('stats').innerHTML = cells.map(function (c) {
      return '<div class="stat"><div class="k">' + c[0] + '</div><div class="v">' + (c[1] == null ? '\\u2014' : c[1]) + '</div></div>';
    }).join('');
    var sc = s.scopeCounts || {};
    document.getElementById('cntQueue').textContent = sc.queue != null ? sc.queue : '\\u2013';
    document.getElementById('cntHoldings').textContent = sc.holdings != null ? sc.holdings : '\\u2013';
    document.getElementById('cntAll').textContent = sc.all != null ? sc.all : '\\u2013';
  }).catch(function (e) { log('summary failed: ' + e.message, 'err'); });
}

loadSummary();
pollThrottled();
pollSec();
</script>
</body>
</html>"""


@app.route('/admin')
def admin_panel():
    return ADMIN_HTML


@app.route('/admin/summary')
def admin_summary():
    """Collection counts + the newest marketData write, for the admin panel header."""
    newest = collection.find_one({'updatedAt': {'$ne': None}}, {'updatedAt': 1}, sort=[('updatedAt', -1)])
    last_updated = newest.get('updatedAt') if newest else None
    custom_tickers = get_custom_tickers()

    def non_custom(values):
        return len({str(t) for t in values if t and str(t) not in custom_tickers})

    return jsonify({
        # what each throttled scope would actually process (custom assets excluded)
        'scopeCounts': {
            'queue': non_custom(tickers_collection.distinct('ticker')),
            'holdings': non_custom(holdings_collection.distinct('ticker')),
            'all': non_custom(collection.distinct('ticker')),
        },
        'queue': tickers_collection.count_documents({}),
        'marketData': collection.count_documents({}),
        'customAssets': custom_assets_collection.count_documents({}),
        'fxRates': db['exchangeRates'].count_documents({}),
        'priceHistory': price_history_collection.count_documents({}),
        'fundamentals': fundamentals_collection.count_documents({}),
        'sharesHistory': shares_history_collection.count_documents({}),
        'lastUpdatedAt': last_updated.isoformat() if last_updated else None,
    }), 200


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
