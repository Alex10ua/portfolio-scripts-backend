import requests
from datetime import datetime, timezone

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.USD+GBP+CHF+PLN+CZK.EUR.SP00.A"
    "?lastNObservations=1&format=jsondata"
)

# ECB series key order: currency codes in the URL above
_CURRENCIES = ["USD", "GBP", "CHF", "PLN", "CZK"]


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
    """Extract currency -> rateVsEur from ECB SDMX-JSON response."""
    rates = {}
    try:
        series = data["dataSets"][0]["series"]
        # series keys are like "0:0:0:0:0", "1:0:0:0:0" — first index = currency position
        for key, series_data in series.items():
            currency_idx = int(key.split(":")[0])
            if currency_idx >= len(_CURRENCIES):
                continue
            currency = _CURRENCIES[currency_idx]
            observations = series_data.get("observations", {})
            if not observations:
                continue
            # Last observation value
            last_obs = observations[max(observations.keys(), key=int)]
            rate = last_obs[0]
            if rate is not None:
                rates[currency] = float(rate)
    except (KeyError, IndexError, TypeError) as e:
        print(f"[ecb_provider] Parse error: {e}")
    return rates
