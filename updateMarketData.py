import yfinance as yf
from pymongo import MongoClient
from datetime import datetime

import updateMarketDataUtilies

# MongoDB connection
client = MongoClient('mongodb://root:secret@localhost:27017/')
db = client['portfolio']  # Replace with your database name
collection = db['marketData']

def insert_or_update_market_data(ticker):
    # Fetch data using yfinance
    stock = yf.Ticker(ticker)

    # Create the data to update
    market_data = {
        'name': updateMarketDataUtilies.get_company_name(stock.info ,ticker),
        'price': updateMarketDataUtilies.get_current_price(stock.info, ticker),
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

# Example usage
if __name__ == "__main__":
    ticker_symbols = ['AAPL', 'MSFT', 'GOOGL', 'ACOMO.AS', 'META', 'BHP', 'CAJPY', 'CKHUY', 'FPAFY']  # Replace with desired ticker symbol
    for ticker_symbol in ticker_symbols:
        insert_or_update_market_data(ticker_symbol)
