from flask import Flask, jsonify, request
from time import sleep
import yfinance as yf
from pymongo import MongoClient
from datetime import datetime
import threading

import updateMarketDataUtilies

# MongoDB connection
client = MongoClient('mongodb://root:secret@localhost:27017/')
db = client['portfolio']  # Replace with your database name
collection = db['marketData']
tickers_collection = db['tickers']
# Global variable to hold our Timer object
debounce_timer = None
# Lock to synchronize access to the timer
lock = threading.Lock()

app = Flask(__name__)


def insert_or_update_market_data(ticker):
    # Fetch data using yfinance
    stock = yf.Ticker(ticker)

    # Create the data to update
    market_data = {
        'name': updateMarketDataUtilies.get_company_name(stock.info ,ticker),
        'price': updateMarketDataUtilies.get_current_price(stock.info, ticker),
        'currency': updateMarketDataUtilies.get_currency(stock.info, ticker),
        'priceYesterday': updateMarketDataUtilies.get_close_price(stock.info, ticker),
        'yearlyDividend': updateMarketDataUtilies.get_yearly_dividend(stock.info, ticker),
        'lastDividendPayment': updateMarketDataUtilies.get_last_dividend_payment(stock.info, ticker),
        'dividends': updateMarketDataUtilies.get_dividends(stock.dividends, ticker),
        'splits': updateMarketDataUtilies.get_splits(stock.splits, ticker),
        'country': updateMarketDataUtilies.get_stock_country(stock.info, ticker),
        'sector': updateMarketDataUtilies.get_sector(stock.info, ticker),
        'industry': updateMarketDataUtilies.get_industry(stock.info, ticker),
        'updatedAt': datetime.now()
    }

    # Update if exists, else insert
    result = collection.update_one(
        {'ticker': ticker},           # Query to find the document
        {'$set': market_data},        # Fields to update
        upsert=True                   # Insert if not found
    )

    if result.matched_count > 0:
        print(f"Updated data for {ticker}")
    else:
        print(f"Inserted new data for {ticker}")

def run_task():
    global debounce_timer
    with lock:
        debounce_timer = None
        # Retrieve ticker symbols from MongoDB
        ticker_symbols = []
        result_ticker_search = tickers_collection.find({})
        for ticker in result_ticker_search:
            ticker_symbol = ticker.get('ticker')
            if ticker_symbol not in ticker_symbols:
                ticker_symbols.append(ticker_symbol)

        total = len(ticker_symbols)
        for i, ticker_symbol in enumerate(ticker_symbols, start=1):
            message = f"Processing ticker {i} of {total}: {ticker_symbol}"
            print(message)
            sleep(1)  # Be aware: this will block the request; consider removing or running asynchronously.
            insert_or_update_market_data(ticker_symbol)
            result = tickers_collection.delete_one({"ticker": ticker_symbol})
            if result.deleted_count > 0:
                print(f"Ticker {ticker_symbol} was deleted.")
            else:
                print(f"Ticker {ticker_symbol} not found.")

    print("Task executed at", threading.current_thread().name)

@app.route('/update_all', methods=['POST'])
def update_all():
    print('triggered')
    global debounce_timer
    with lock:
        # If a timer is already running, cancel it
        if debounce_timer is not None:
            debounce_timer.cancel()
        # Create a new timer that will run the task after 2 minutes (120 seconds)
        debounce_timer = threading.Timer(10, run_task())
        debounce_timer.start()
    return '', 202

@app.route('/update_one', methods=['POST'])
def update_one():
    data = request.get_json()
    ticker = data['ticker']
    insert_or_update_market_data(ticker)
    return jsonify({"status": "received", "ticker": ticker}), 200

if __name__ == '__main__':
    app.run(debug=True)
