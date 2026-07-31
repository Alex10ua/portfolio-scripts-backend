from datetime import datetime, timezone


def get_yearly_dividend(stock_info, ticker):
    try:
        yearly_dividend = stock_info.get('dividendRate') or ''
    except Exception as e:
        print('Error getting yearly dividend for {}: {}'.format(ticker, e))
        yearly_dividend = None
    return yearly_dividend

def get_last_dividend_payment(stock_info, ticker):
    try:
        last_div_payment = stock_info.get('lastDividendValue') or ''
    except Exception as e:
        print('Error getting last dividend for {}: {}'.format(ticker, e))
        last_div_payment = None
    return last_div_payment

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
        price_at_close = stock_info.get('previousClose') or stock_info.get('currentPrice')
    except Exception as e:
        print(f"Error getting price at close for {ticker}: {e}")
        price_at_close = None
    return price_at_close

def get_dividends(dividends_series, ticker):
    try:
        dividends = [
            {'dividendDate': date, 'dividendAmount': dividend}
            for date, dividend in dividends_series.items()
        ] or []

    except Exception as e:
        print(f"Error getting dividends for {ticker}: {e}")
        dividends = []
    return dividends

def get_splits(splits_series, ticker):
    try:
        splits = [
        {'splitDate': date, 'ratioSplit': split}
           for date, split in splits_series.items()
        ] or []
    except Exception as e:
           print(f"Error getting splits for {ticker}: {e}")
           splits = []
    return splits

def get_stock_country(stock_info, ticker):
    try:
        country = stock_info.get('country') or ''
    except Exception as e:
        print('Error getting country for {}: {}'.format(ticker, e))
        country = None
    return country

def get_sector(stock_info, ticker):
    try:
        sector = stock_info.get('sector') or ''
    except Exception as e:
        print('Error getting sector for {}: {}'.format(ticker, e))
        sector = None
    return sector

def get_industry(stock_info, ticker):
    try:
        industry = stock_info.get('industry') or ''
    except Exception as e:
        print('Error getting industry for {}: {}'.format(ticker, e))
        industry = None
    return industry

def get_currency(stock_info, ticker):
    try:
        currency = stock_info.get('currency')
    except Exception as e:
        print('Error getting currency for {}: {}'.format(ticker, e))
        currency = None
    return currency

def get_shares_outstanding(stock_info, ticker):
    try:
        shares = stock_info.get('sharesOutstanding') or stock_info.get('impliedSharesOutstanding')
        return int(shares) if shares else None
    except Exception as e:
        print('Error getting shares outstanding for {}: {}'.format(ticker, e))
        return None


# ---------- Key statistics (Yahoo .info -> marketData.statistics) ----------
#
# (output key, yfinance info key, kind). Output keys are the field names of the
# Java MarketStatistics model, so renaming one here means renaming it there too.
# kinds: 'num' float | 'int' whole number | 'date' epoch seconds -> 'YYYY-MM-DD'
#        | 'str' passthrough.
# Values are stored exactly as Yahoo reports them — no rescaling here, formatting
# is the UI's job. Yahoo's own scaling is inconsistent: margins/growth/returns are
# fractions (0.3934 = 39.34%), but dividendYield, fiveYearAvgDividendYield and
# debtToEquity already come as percentages (0.93 = 0.93%, 30.27 = 30.27%).
STATISTICS_FIELDS = [
    # Fiscal year
    ('fiscalYearEnd', 'lastFiscalYearEnd', 'date'),
    ('mostRecentQuarter', 'mostRecentQuarter', 'date'),

    # Profitability
    ('profitMargin', 'profitMargins', 'num'),
    ('operatingMargin', 'operatingMargins', 'num'),
    ('grossMargin', 'grossMargins', 'num'),
    ('ebitdaMargin', 'ebitdaMargins', 'num'),

    # Management effectiveness
    ('returnOnAssets', 'returnOnAssets', 'num'),
    ('returnOnEquity', 'returnOnEquity', 'num'),

    # Income statement (ttm)
    ('revenue', 'totalRevenue', 'int'),
    ('revenuePerShare', 'revenuePerShare', 'num'),
    ('revenueGrowth', 'revenueGrowth', 'num'),
    ('grossProfit', 'grossProfits', 'int'),
    ('ebitda', 'ebitda', 'int'),
    ('netIncomeToCommon', 'netIncomeToCommon', 'int'),
    ('dilutedEps', 'trailingEps', 'num'),
    ('forwardEps', 'forwardEps', 'num'),
    ('earningsQuarterlyGrowth', 'earningsQuarterlyGrowth', 'num'),
    ('earningsGrowth', 'earningsGrowth', 'num'),

    # Balance sheet (mrq)
    ('totalCash', 'totalCash', 'int'),
    ('totalCashPerShare', 'totalCashPerShare', 'num'),
    ('totalDebt', 'totalDebt', 'int'),
    ('debtToEquity', 'debtToEquity', 'num'),
    ('currentRatio', 'currentRatio', 'num'),
    ('quickRatio', 'quickRatio', 'num'),
    ('bookValuePerShare', 'bookValue', 'num'),

    # Cash flow (ttm)
    ('operatingCashflow', 'operatingCashflow', 'int'),
    ('freeCashflow', 'freeCashflow', 'int'),

    # Valuation measures
    ('marketCap', 'marketCap', 'int'),
    ('enterpriseValue', 'enterpriseValue', 'int'),
    ('trailingPE', 'trailingPE', 'num'),
    ('forwardPE', 'forwardPE', 'num'),
    ('pegRatio', 'trailingPegRatio', 'num'),
    ('priceToSales', 'priceToSalesTrailing12Months', 'num'),
    ('priceToBook', 'priceToBook', 'num'),
    ('enterpriseToRevenue', 'enterpriseToRevenue', 'num'),
    ('enterpriseToEbitda', 'enterpriseToEbitda', 'num'),

    # Trading / price stats
    ('beta', 'beta', 'num'),
    ('fiftyTwoWeekHigh', 'fiftyTwoWeekHigh', 'num'),
    ('fiftyTwoWeekLow', 'fiftyTwoWeekLow', 'num'),
    ('fiftyTwoWeekChange', '52WeekChange', 'num'),
    ('sp500FiftyTwoWeekChange', 'SandP52WeekChange', 'num'),
    ('fiftyDayAverage', 'fiftyDayAverage', 'num'),
    ('twoHundredDayAverage', 'twoHundredDayAverage', 'num'),
    ('volume', 'volume', 'int'),
    ('averageVolume', 'averageVolume', 'int'),
    ('averageVolume10days', 'averageVolume10days', 'int'),

    # Share statistics
    ('sharesOutstanding', 'sharesOutstanding', 'int'),
    ('impliedSharesOutstanding', 'impliedSharesOutstanding', 'int'),
    ('floatShares', 'floatShares', 'int'),
    ('sharesShort', 'sharesShort', 'int'),
    ('sharesShortPriorMonth', 'sharesShortPriorMonth', 'int'),
    ('shortRatio', 'shortRatio', 'num'),
    ('shortPercentOfFloat', 'shortPercentOfFloat', 'num'),
    ('heldPercentInsiders', 'heldPercentInsiders', 'num'),
    ('heldPercentInstitutions', 'heldPercentInstitutions', 'num'),

    # Dividends & splits
    ('dividendRate', 'dividendRate', 'num'),
    ('dividendYield', 'dividendYield', 'num'),
    ('trailingAnnualDividendRate', 'trailingAnnualDividendRate', 'num'),
    ('trailingAnnualDividendYield', 'trailingAnnualDividendYield', 'num'),
    ('fiveYearAvgDividendYield', 'fiveYearAvgDividendYield', 'num'),
    ('payoutRatio', 'payoutRatio', 'num'),
    ('exDividendDate', 'exDividendDate', 'date'),
    ('nextDividendDate', 'dividendDate', 'date'),
    ('lastSplitFactor', 'lastSplitFactor', 'str'),
    ('lastSplitDate', 'lastSplitDate', 'date'),

    # Analyst view
    ('targetHighPrice', 'targetHighPrice', 'num'),
    ('targetLowPrice', 'targetLowPrice', 'num'),
    ('targetMeanPrice', 'targetMeanPrice', 'num'),
    ('recommendationMean', 'recommendationMean', 'num'),
    ('recommendationKey', 'recommendationKey', 'str'),
    ('numberOfAnalystOpinions', 'numberOfAnalystOpinions', 'int'),

    # Company profile
    ('fullTimeEmployees', 'fullTimeEmployees', 'int'),
    ('exchange', 'exchange', 'str'),
    ('quoteType', 'quoteType', 'str'),
    ('website', 'website', 'str'),
]


def _epoch_to_date(value):
    """Yahoo reports fiscal/dividend/split dates as epoch seconds. Returns
    'YYYY-MM-DD' (UTC) — the format the Java LocalDate fields parse."""
    return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime('%Y-%m-%d')


def _coerce_statistic(value, kind):
    """None means 'no usable value' — the caller drops the key entirely rather
    than writing a null over whatever the previous update stored."""
    if value is None or value == '' or isinstance(value, bool):
        return None
    if kind == 'str':
        text = str(value).strip()
        return text or None
    if kind == 'date':
        return _epoch_to_date(value)
    if kind == 'int':
        return int(float(value))
    return float(value)


def get_statistics(stock_info, ticker):
    """
    Extract the full key-statistics set from a Yahoo .info dict into a flat
    dict for marketData.statistics. Missing/unparsable fields are omitted, so a
    thin provider response never blanks out a previously-complete snapshot
    (marketData writes $set the whole statistics sub-document at once).
    Returns None when nothing could be extracted.
    """
    stats = {}
    for out_key, info_key, kind in STATISTICS_FIELDS:
        try:
            coerced = _coerce_statistic(stock_info.get(info_key), kind)
        except (TypeError, ValueError, OSError, OverflowError) as e:
            print(f'Error reading statistic {info_key} for {ticker}: {e}')
            continue
        if coerced is not None:
            stats[out_key] = coerced
    if not stats:
        return None
    stats['updatedAt'] = datetime.now().strftime('%Y-%m-%d')
    return stats