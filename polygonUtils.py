from polygon import RESTClient
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("API_KEY")
client = RESTClient(api_key)

def getTickerData(ticker):
	"""
	Fetch the latest ticker data for a given ticker symbol.
	"""
	try:
		ticker_data = client.get_ticker_details(ticker = ticker)
		return ticker_data.name
	except Exception as e:
		print(f"Error fetching ticker data for {ticker}: {e}")
		return None

def getPrice(ticker):
	"""
	Fetch the latest price for a given ticker symbol.
	"""
	try:
		ticker_data = client.get_snapshot_ticker(ticker = ticker)
		price = ticker_data.last_quote.ask_price
		price_yesterday = ticker_data.prev_day.close
		return price, price_yesterday
	except Exception as e:
		print(f"Error fetching price for {ticker}: {e}")
		return None

def getDividends(ticker, date_from = None):
	"""
	Fetch dividends for a given ticker symbol.
	"""
	dividends = []
	list_dividends = []
	yearly_dividend = 0
	last_dividend_amount = 0
	try:
		if date_from is None:
			list_dividends = client.list_dividends(
				ticker = ticker,
				order="asc",
				limit=100,
				sort="ex_dividend_date",
			)
		else :
			list_dividends = client.list_dividends(
				ticker = ticker,
				pay_date_gt=date_from,
				order="asc",
				limit=100,
				sort="ex_dividend_date",
			)
		for dividend in list_dividends:
			current_dividend = {
				"exDividendDate": dividend.ex_dividend_date,
				"dividendDate": dividend.pay_date,
				"dividendAmount": dividend.cash_amount
			}
			if dividend == list_dividends[-1]:
				yearly_dividend = round(dividend.cash_amount * dividend.frequency, 2)
				last_dividend_amount = dividend.cash_amount
			dividends.append(current_dividend)

		return dividends, yearly_dividend, last_dividend_amount
	except Exception as e:
		print(f"Error fetching dividends for {ticker}: {e}")
		return None


def getSplits(ticker, date_from = None):
	"""
	Fetch splits for a given ticker symbol.
	"""
	splits = []
	list_splits = []
	try:
		if date_from is None:
			list_splits = client.list_splits(
				ticker = ticker,
				order="desc",
				limit=100,
				sort="execution_date",
			)
		else:
			list_splits = client.list_splits(
				ticker = ticker,
				execution_date_gt=date_from,
				order="desc",
				limit=100,
				sort="execution_date",
			)
		for split in client.list_splits(
			ticker = ticker,
			order="desc",
			limit=100,
			sort="execution_date",
		):
			current_split = {
				"splitDate": split.execution_date,
				"ratioSplit": round(split.split_to/split.split_from, 2)
			}
			splits.append(current_split)
		return splits
	except Exception as e:
		print(f"Error fetching splits for {ticker}: {e}")
		return None