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

The same file also carries ratio_overrides, for the opposite case: a real split
whose ratio Yahoo reports at too few digits. Unilever's 8-for-9 consolidation
(2025-12-09) arrives as 0.888, not 8/9 = 0.888888…, and that truncation quietly
shaves 0.0053 off every 6 shares held. An override replaces the ratio at the same
ingest funnel, so the corrected value is what reaches marketData.splits — and
_merge_list overwrites same-day entries, so one refresh heals a stored ratio.

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
_overrides_cache: list | None = None


def _day_key(value) -> str:
    """
    'YYYY-MM-DD' from a tz-aware Timestamp, a naive datetime or a string. Same
    normalization _merge_list uses — yfinance yields Timestamps, Mongo returns
    naive datetimes, the JSON file holds strings, and none compare equal raw.
    """
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return str(value)[:10]


def _read_section(section: str) -> list:
    """One section of the JSON file, entries missing ticker/action_date dropped.
    Never raises: a broken file must not take the market-data updater down."""
    with open(_IGNORE_FILE, 'r', encoding='utf-8') as handle:
        entries = (json.load(handle) or {}).get(section) or []
    return [e for e in entries if e.get('ticker') and e.get('action_date')]


def load_ignored(force_reload: bool = False) -> list:
    """The ignore list, read once per process. Degrades to 'ignore nothing' + a log."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    try:
        _cache = _read_section('ignored')
    except Exception as e:
        print(f'[corporate_actions] Could not read {_IGNORE_FILE}: {e} — ignoring nothing')
        _cache = []
    return _cache


def load_ratio_overrides(force_reload: bool = False) -> list:
    """The ratio-override list, read once per process. Degrades to 'override nothing'."""
    global _overrides_cache
    if _overrides_cache is not None and not force_reload:
        return _overrides_cache
    try:
        _overrides_cache = _read_section('ratio_overrides')
    except Exception as e:
        print(f'[corporate_actions] Could not read {_IGNORE_FILE}: {e} — overriding nothing')
        _overrides_cache = []
    return _overrides_cache


def override_ratio(entry: dict):
    """
    The exact ratio an override entry specifies, or None if it specifies none usable.

    A fraction is preferred over exact_ratio: 8/9 written out as a decimal is a typo
    waiting to happen, and the numerator/denominator are what the RNS actually states.
    """
    numerator, denominator = entry.get('ratio_numerator'), entry.get('ratio_denominator')
    try:
        if numerator is not None and denominator:
            return float(numerator) / float(denominator)
        if entry.get('exact_ratio') is not None:
            return float(entry['exact_ratio'])
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def find_ratio_override(ticker: str, action_date, ratio) -> dict | None:
    """The ratio-override entry matching this action, or None. Same matching rules as
    find_ignored — ticker + calendar day, and yf_ratio when the entry carries one."""
    return _find_entry(load_ratio_overrides(), ticker, action_date, ratio)


def find_ignored(ticker: str, action_date, ratio) -> dict | None:
    """
    The ignore-list entry matching this action, or None.

    Matches on ticker + calendar day, and on yf_ratio when the entry carries one.
    A different ratio on the same date deliberately does NOT match: that is a new,
    unreviewed action, not the one that was signed off.
    """
    return _find_entry(load_ignored(), ticker, action_date, ratio)


def _find_entry(entries: list, ticker: str, action_date, ratio) -> dict | None:
    """Shared matcher for both sections: ticker + calendar day, plus yf_ratio when given."""
    if not ticker:
        return None
    day = _day_key(action_date)
    for entry in entries:
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

        override = find_ratio_override(ticker, date, ratio)
        if override is not None:
            exact = override_ratio(override)
            if exact is not None:
                print(f"[corporate_actions] {ticker}: ratio {ratio} on {_day_key(date)} overridden "
                      f"to {exact!r} ({override.get('note') or 'listed in ignored_splits.json'})")
                split = {**split, 'ratioSplit': exact}
                kept.append(split)
                # An overridden ratio has been reviewed — warning about it again is noise.
                continue

        if is_suspicious(ratio):
            print(f"[corporate_actions] WARNING {ticker}: split ratio {ratio} on {_day_key(date)} "
                  f"is in the spin-off band ({SUSPICIOUS_LOW}-{SUSPICIOUS_HIGH}) and is not in "
                  f"ignored_splits.json — applied as a real split. Add an entry if it is not one.")
        kept.append(split)
    return kept
