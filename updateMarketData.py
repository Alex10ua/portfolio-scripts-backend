from flask import Flask, jsonify, request
from time import sleep
import yfinance as yf
from pymongo import MongoClient, UpdateOne, DeleteOne
from datetime import datetime
import threading
import concurrent.futures

import updateMarketDataUtilities

# MongoDB connection
client = MongoClient('mongodb://root:secret@mongodb:27017/')
db = client['portfolio']  # Replace with your database name
collection = db['marketData']
tickers_collection = db['tickers']
# Global variable to hold our Timer object
debounce_timer = None
# Lock to synchronize access to the timer
lock = threading.Lock()

app = Flask(__name__)


def process_ticker(ticker: str) -> tuple[str, UpdateOne | None, str | None]:
    """
    Fetches data for a single ticker and returns the update operation.
    Returns: (ticker, data/update_op, error_message)
    """
    try:
        # Fetch data using yfinance
        stock = yf.Ticker(ticker)
        # Accessing info triggers the fetch
        info = stock.info
        
        # Create the data to update
        market_data = {
            'name': updateMarketDataUtilies.get_company_name(info, ticker),
            'price': updateMarketDataUtilies.get_current_price(info, ticker),
            'currency': updateMarketDataUtilies.get_currency(info, ticker),
            'priceYesterday': updateMarketDataUtilies.get_close_price(info, ticker),
            'yearlyDividend': updateMarketDataUtilies.get_yearly_dividend(info, ticker),
            'lastDividendPayment': updateMarketDataUtilies.get_last_dividend_payment(info, ticker),
            'dividends': updateMarketDataUtilies.get_dividends(stock.dividends, ticker),
            'splits': updateMarketDataUtilies.get_splits(stock.splits, ticker),
            'country': updateMarketDataUtilies.get_stock_country(info, ticker),
            'sector': updateMarketDataUtilies.get_sector(info, ticker),
            'industry': updateMarketDataUtilies.get_industry(info, ticker),
            'updatedAt': datetime.now()
        }

        # Create UpdateOne operation for bulk write
        operation = UpdateOne(
            {'ticker': ticker},           # Query to find the document
            {'$set': market_data},        # Fields to update
            upsert=True                   # Insert if not found
        )
        return ticker, operation, None

    except Exception as e:
        return ticker, None, str(e)


def insert_or_update_market_data(ticker):
    """
    Legacy wrapper for single update, now uses the helper and executes immediately.
    """
    ticker_result, operation, error = process_ticker(ticker)
    
    if operation:
        try:
            result = collection.bulk_write([operation])
            if result.matched_count > 0:
                print(f"Updated data for {ticker}")
            else:
                print(f"Inserted new data for {ticker}")
        except Exception as e:
            print(f"Error writing to DB for {ticker}: {e}")
    else:
        print(f"Failed to fetch data for {ticker}: {error}")


def run_task():
    global debounce_timer
    with lock:
        debounce_timer = None
    
    print(f"[{datetime.now()}] Starting batch processing task...")

    # Retrieve ticker symbols from MongoDB
    # Use projection to get only the ticker field
    cursor = tickers_collection.find({}, {'ticker': 1})
    # Use a set to avoid duplicates immediately
    ticker_symbols: list[str] = list(set(str(doc.get('ticker')) for doc in cursor if doc.get('ticker')))

    total = len(ticker_symbols)
    if total == 0:
        print("No tickers to process.")
        return

    print(f"Found {total} unique tickers to process.")

    updates = []
    deletes = []
    
    # Use ThreadPoolExecutor for parallel fetching
    # Adjust max_workers as needed (network bound)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # process_ticker signature is specific, triggerring a strict type check error with submit
        future_to_ticker = {executor.submit(process_ticker, t): t for t in ticker_symbols}  # type: ignore
        
        for i, future in enumerate(concurrent.futures.as_completed(future_to_ticker), 1):
            ticker = future_to_ticker[future]
            try:
                # Get result
                processed_ticker, operation, error = future.result()
                
                if operation:
                    updates.append(operation)
                    deletes.append(DeleteOne({'ticker': processed_ticker}))
                    print(f"[{i}/{total}] Successfully fetched: {processed_ticker}")
                else:
                    print(f"[{i}/{total}] Failed to fetch: {processed_ticker} - Error: {error}")
                    
            except Exception as e:
                print(f"[{i}/{total}] Exception processing {ticker}: {e}")

    # Bulk execute updates
    if updates:
        try:
            print(f"Writing {len(updates)} updates to 'marketData' collection...")
            result = collection.bulk_write(updates)
            print(f"Bulk write result: Matched={result.matched_count}, Modified={result.modified_count}, Upserted={result.upserted_count}")
        except Exception as e:
            print(f"Error during bulk update: {e}")

    # Bulk execute deletes
    if deletes:
        try:
            print(f"Removing {len(deletes)} processed tickers from 'tickers' queue...")
            result = tickers_collection.bulk_write(deletes)
            print(f"Bulk delete result: Deleted={result.deleted_count}")
        except Exception as e:
            print(f"Error during bulk delete: {e}")

    print(f"[{datetime.now()}] Task finished.")


@app.route('/update_all', methods=['POST'])
def update_all():
    print('Update all triggered')
    global debounce_timer
    with lock:
        # If a timer is already running, cancel it
        if debounce_timer is not None:
            debounce_timer.cancel()
        # Create a new timer that will run the task after 10 seconds
        # FIXED: Pass the function `run_task`, not the result of `run_task()`
        debounce_timer = threading.Timer(10, run_task)
        debounce_timer.start()
    return jsonify({"status": "debounced", "message": "Task scheduled in 10 seconds"}), 202


@app.route('/update_one', methods=['POST'])
def update_one_route(): # Renamed to avoid collision
    data = request.get_json()
    ticker = data.get('ticker')
    if not ticker:
        return jsonify({"error": "No ticker provided"}), 400
        
    insert_or_update_market_data(ticker)
    return jsonify({"status": "received", "ticker": ticker}), 200

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
