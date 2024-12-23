
def get_yearly_dividend(stock_info, ticker):
    try:
        yearly_dividend = stock_info.get('dividendRate')
    except Exception as e:
        print('Error getting yearly dividend for {}: {}'.format(ticker, e))
        yearly_dividend = None
    return yearly_dividend

def get_last_dividend_payment(stock_info, ticker):
    try:
        last_div_payment = stock_info.get('lastDividendValue')
    except Exception as e:
        print('Error getting last dividend for {}: {}'.format(ticker, e))
        last_div_payment = None
    return last_div_payment

def get_dividend_frequency(stock_info, ticker):
    try:
        dividend_frequency = stock_info.get('dividendRate') % stock_info.get('lastDividendValue')
    except Exception as e:
        print('Error getting company name for {}: {}'.format(ticker, e))
        dividend_frequency = None
    return dividend_frequency

def get_company_name(stock_info, ticker):
    try:
        name = stock_info.get('longName') or stock_info.get('shortName') or ''
    except Exception as e:
        print(f"Error getting company name for {ticker}: {e}")
        name = ''
    return name

def get_current_price(stock_info, ticker):
    try:
        price = stock_info.get('currentPrice') or ''
    except Exception as e:
        print(f"Error getting current price for {ticker}: {e}")
        price = None
    return price

def get_close_price(stock_info, ticker):
    try:
        price_at_close = stock_info.get('previousClose') or ''
    except Exception as e:
        print(f"Error getting price at close for {ticker}: {e}")
        price_at_close = None
    return price_at_close

def get_dividends(dividends_series, ticker):
    try:
        dividends = [
            {'dividendDate': date, 'dividendAmount': dividend}
            for date, dividend in dividends_series.items()
        ]

    except Exception as e:
        print(f"Error getting dividends for {ticker}: {e}")
        dividends = []
    return dividends

def get_splits(splits_series, ticker):
    try:
        splits = [
        {'splitDate': date, 'ratioSplit': split}
           for date, split in splits_series.items()
        ]
    except Exception as e:
           print(f"Error getting splits for {ticker}: {e}")
           splits = []
    return splits

def get_stock_country(stock_info, ticker):
    try:
        county = stock_info.get('country')
    except Exception as e:
        print('Error getting country for {}: {}'.format(ticker, e))
        county = None
    return county

def get_sector(stock_info, ticker):
    try:
        sector = stock_info.get('sector')
    except Exception as e:
        print('Error getting sectore for {}: {}'.format(ticker, e))
        sector = None
    return sector

def get_industry(stock_info, ticker):
    try:
        industry = stock_info.get('industry')
    except Exception as e:
        print('Error getting industry for {}: {}'.format(ticker, e))
        industry = None
    return industry