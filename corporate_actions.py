"""
Filter for corporate actions Yahoo files under "Stock Splits" that are not splits.

Yahoo carries every price-basis-changing action in one field and yfinance surfaces
it with no type discriminator, so a spin-off is indistinguishable from a split at
the API level. Applying one as a split corrupts share counts: SPGI's 1.057
spin-off ratio turned a 5-share position into 5.228.

Entries listed in ignored_splits.json are dropped at ingest, before anything is
written to marketData.splits. Nothing downstream then applies them. Transaction
prices and average cost are never rewritten — dropping the action just leaves the
booked figures as they are.

Pinned to yfinance 0.2.66 (see requirements.txt): the ratio arrives as the
'Stock Splits' column of history(), and the auto_adjust default has moved between
versions, so every call site passes it explicitly.
"""

import json
import math
import os

_IGNORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ignored_splits.json')

# Ratios in this band are almost never real splits — boards declare splits in round
# terms (2:1, 3:2), while a spin-off ratio is a computed price quotient landing just
# off 1.0. Unlisted ratios in here get a warning at ingest, not a rejection.
SUSPICIOUS_LOW = 0.85
SUSPICIOUS_HIGH = 1.18

# Ratios are stored rounded (1.057) while the provider may hand back 1.0570000345.
_RATIO_TOLERANCE = 1e-3

_cache: list | None = None


def _day_key(value) -> str:
    """
    'YYYY-MM-DD' from a tz-aware Timestamp, a naive datetime or a string. Same
    normalization _merge_list uses — yfinance yields Timestamps, Mongo returns
    naive datetimes, the JSON file holds strings, and none compare equal raw.
    """
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return str(value)[:10]


def load_ignored(force_reload: bool = False) -> list:
    """The ignore list, read once per process. Never raises: a broken file must not
    take the market-data updater down, so it degrades to 'ignore nothing' + a log."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    try:
        with open(_IGNORE_FILE, 'r', encoding='utf-8') as handle:
            entries = (json.load(handle) or {}).get('ignored') or []
        _cache = [e for e in entries if e.get('ticker') and e.get('action_date')]
    except Exception as e:
        print(f'[corporate_actions] Could not read {_IGNORE_FILE}: {e} — ignoring nothing')
        _cache = []
    return _cache


def find_ignored(ticker: str, action_date, ratio) -> dict | None:
    """
    The ignore-list entry matching this action, or None.

    Matches on ticker + calendar day, and on yf_ratio when the entry carries one.
    A different ratio on the same date deliberately does NOT match: that is a new,
    unreviewed action, not the one that was signed off.
    """
    if not ticker:
        return None
    day = _day_key(action_date)
    for entry in load_ignored():
        if entry['ticker'].upper() != ticker.upper():
            continue
        if _day_key(entry['action_date']) != day:
            continue
        expected = entry.get('yf_ratio')
        if expected is None:
            return entry
        try:
            if math.isclose(float(ratio), float(expected), rel_tol=_RATIO_TOLERANCE):
                return entry
        except (TypeError, ValueError):
            continue
    return None


def is_suspicious(ratio) -> bool:
    """True for a ratio in the spin-off band that is not an exact 1.0 no-op."""
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return False
    if math.isclose(value, 1.0, rel_tol=_RATIO_TOLERANCE):
        return False
    return SUSPICIOUS_LOW < value < SUSPICIOUS_HIGH


def filter_splits(splits: list, ticker: str) -> list:
    """
    Drop listed non-split actions from a [{'splitDate', 'ratioSplit'}] list.

    Warns — never fails — on an unlisted ratio in the suspicious band: the job
    keeps running, and the log line names ticker, date and ratio so the action can
    be added to ignored_splits.json if it turns out to be another spin-off.
    """
    kept = []
    for split in splits or []:
        date, ratio = split.get('splitDate'), split.get('ratioSplit')
        entry = find_ignored(ticker, date, ratio)
        if entry:
            print(f"[corporate_actions] {ticker}: ignoring {entry.get('action_type', 'OTHER')} "
                  f"{ratio} on {_day_key(date)} ({entry.get('note') or 'listed in ignored_splits.json'})")
            continue
        if is_suspicious(ratio):
            print(f"[corporate_actions] WARNING {ticker}: split ratio {ratio} on {_day_key(date)} "
                  f"is in the spin-off band ({SUSPICIOUS_LOW}-{SUSPICIOUS_HIGH}) and is not in "
                  f"ignored_splits.json — applied as a real split. Add an entry if it is not one.")
        kept.append(split)
    return kept
