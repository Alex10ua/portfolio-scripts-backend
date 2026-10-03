import requests
from datetime import datetime, timezone

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.USD+GBP+CHF+PLN+CZK.EUR.SP00.A"
    "?lastNObservations=1&format=jsondata"
)


def fetch_and_store_rates(db) -> dict:
    """Fetch latest ECB FX rates and upsert into the exchangeRates collection.

    Rates are expressed as units of each currency per 1 EUR (EUR is base = 1.0).
    Returns a dict mapping currency -> rateVsEur.
    """
    collection = db["exchangeRates"]
    response = requests.get(ECB_URL, timeout=10)
    response.raise_for_status()

    data = response.json()
    rates = _parse_ecb_response(data)
    if not rates:
        # Storing EUR alone and answering 'ok' is how a broken parse went unnoticed:
        # every missing currency silently converts at 1.0 downstream.
        raise RuntimeError("ECB response carried no parsable rates")

    # Always include EUR itself
    rates["EUR"] = 1.0

    # exchangeRates document schema (must match Java FxRate model):
    # { _id: str, currency: str, rateVsEur: float, date: str (YYYY-MM-DD), updatedAt: datetime (UTC) }
    now = datetime.now(timezone.utc)
    for currency, rate in rates.items():
        collection.update_one(
            {"_id": currency},
            {"$set": {
                "currency": currency,
                "rateVsEur": rate,
                "date": now.strftime("%Y-%m-%d"),
                "updatedAt": now,
            }},
            upsert=True,
        )

    print(f"[ecb_provider] Stored rates for: {list(rates.keys())}")
    return rates


def _parse_ecb_response(data: dict) -> dict:
    """
    Extract currency -> rateVsEur from an ECB SDMX-JSON response.

    A series key such as "0:3:0:0:0" holds one index per series dimension, in the
    order structure.dimensions.series lists them — FREQ, CURRENCY, CURRENCY_DENOM,
    EXR_TYPE, EXR_SUFFIX — and each index points into that dimension's own `values`
    list. So the currency is neither the key's first position (that is FREQ, always
    0 for a daily query) nor the order the URL asked for: the ECB lists the values
    alphabetically (CHF, CZK, GBP, PLN, USD). Reading position 0 mapped every
    series onto one currency, and only USD was ever stored.
    """
    rates = {}
    try:
        dimensions = data["structure"]["dimensions"]["series"]
        position = next(i for i, dim in enumerate(dimensions) if dim.get("id") == "CURRENCY")
        codes = [value["id"] for value in dimensions[position]["values"]]
        for key, series_data in data["dataSets"][0]["series"].items():
            currency = codes[int(key.split(":")[position])]
            observations = series_data.get("observations") or {}
            if not observations:
                continue
            # Last observation value
            rate = observations[max(observations, key=int)][0]
            if rate is not None:
                rates[currency] = float(rate)
    except (KeyError, IndexError, TypeError, ValueError, StopIteration) as e:
        print(f"[ecb_provider] Parse error: {e}")
    return rates
