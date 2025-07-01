from flask import Flask, jsonify, request
from time import sleep
import yfinance as yf
from pymongo import MongoClient
from datetime import datetime
import threading
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

import updateMarketDataUtilies

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


### MODIFIED HELPER FUNCTION ###
def _format_timeseries_data(series: pd.Series, date_col_name: str, value_col_name: str) -> list:
    """Formats a pandas Series into a list of dictionaries for MongoDB."""
    if series.empty:
        return []
    df = series.reset_index()
    # Ensure standard column names and correct dtypes for JSON serialization
    df.columns = [date_col_name, value_col_name]
    df[date_col_name] = pd.to_datetime(df[date_col_name])
    if isinstance(df[value_col_name].dtype, (np.integer, np.floating)):
         df[value_col_name] = df[value_col_name].astype(float) # Convert numpy types to native Python types
    return df.to_dict('records')

### NEW HELPER FUNCTION FOR PRICE HISTORY ###
def _format_history_data(df: pd.DataFrame) -> list:
    """Formats a history DataFrame into a list of dictionaries for MongoDB."""
    if df.empty:
        return []
    df = df.reset_index()
    # Rename columns to be database-friendly (lowercase)
    df.columns = [col.lower() for col in df.columns]
    # Select only the columns we want to store
    df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
    # Convert numpy types to native Python types for JSON serialization
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    df['date'] = pd.to_datetime(df['date'])
    return df.to_dict('records')

def insert_or_update_market_data(ticker):
    """
        Fetches, inserts, or incrementally updates market data for a given ticker.
        - Static data is updated on every run.
        - Time-series data (price history, dividends, splits) is updated incrementally.
        """
    stock = yf.Ticker(ticker)

    # 1. Update static/current data using $set
    market_data_static = {
        'name': updateMarketDataUtilies.get_company_name(stock.info ,ticker),
        'price': updateMarketDataUtilies.get_current_price(stock.info, ticker),
        'currency': updateMarketDataUtilies.get_currency(stock.info, ticker),
        'priceYesterday': updateMarketDataUtilies.get_close_price(stock.info, ticker),
        'yearlyDividend': updateMarketDataUtilies.get_yearly_dividend(stock.info, ticker),
        'lastDividendPayment': updateMarketDataUtilies.get_last_dividend_payment(stock.info, ticker),
        'country': updateMarketDataUtilies.get_stock_country(stock.info, ticker),
        'sector': updateMarketDataUtilies.get_sector(stock.info, ticker),
        'industry': updateMarketDataUtilies.get_industry(stock.info, ticker),
        'updatedAt': datetime.now()
    }
    collection.update_one({'ticker': ticker}, {'$set': market_data_static}, upsert=True)
    print(f"Upserted static data for {ticker}")

    # --- INCREMENTAL UPDATES ---

    # 2. Incrementally update Price History
    last_history_entry = collection.find_one(
        {'ticker': ticker, 'history': {'$exists': True, '$ne': []}},
        {'history.date': 1, '_id': 0},
        sort=[('history.date', -1)]
    )

    history_start_date = None
    if last_history_entry:
        last_date = last_history_entry['history'][0]['date']
        history_start_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
        print(
            f"Found last price history for {ticker} on {last_date.date()}. Fetching new data from {history_start_date}.")

    # Fetch new price data from yfinance using the start date
    new_history_df = stock.history(start=history_start_date,
                                   auto_adjust=False)  # auto_adjust=False is important to get splits/dividends columns
    new_history_list = _format_history_data(new_history_df)

    if new_history_list:
        collection.update_one(
            {'ticker': ticker},
            {'$push': {'history': {'$each': sorted(new_history_list, key=lambda x: x['date'])}}}
        )
        print(f"Added {len(new_history_list)} new price history record(s) for {ticker}.")
    else:
        print(f"No new price history to update for {ticker}.")

    # 3. Incrementally update Dividends and Splits
    for data_key, yfinance_series, date_col, val_col in [
        ('dividends', stock.dividends, 'date', 'amount'),
        ('splits', stock.splits, 'date', 'ratio')
    ]:
        last_entry = collection.find_one(
            {'ticker': ticker, data_key: {'$exists': True, '$ne': []}},
            {f'{data_key}.{date_col}': 1, '_id': 0},
            sort=[(f'{data_key}.{date_col}', -1)]
        )

        start_date = None
        if last_entry and data_key in last_entry and last_entry[data_key]:
            last_date = last_entry[data_key][0][date_col]
            start_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')

        if start_date:
            new_data_series = yfinance_series[yfinance_series.index >= start_date]
        else:
            new_data_series = yfinance_series
        new_data_list = _format_timeseries_data(new_data_series, date_col, val_col)

        if new_data_list:
            # Ensure data is sorted by date before pushing
            sorted_new_data = sorted(new_data_list, key=lambda x: x[date_col])
            collection.update_one(
                {'ticker': ticker},
                {'$push': {data_key: {'$each': sorted_new_data}}}
            )
            print(f"Added {len(new_data_list)} new {data_key} record(s) for {ticker}.")
        else:
            print(f"No new {data_key} data to update for {ticker}.")

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
    app.run(debug=True, host='0.0.0.0')
