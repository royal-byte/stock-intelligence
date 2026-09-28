# /// script
# requires-python = ">=3.10"
# dependencies = ["yfinance"]
# ///
"""Analyst Adapter: sell-side consensus + valuation snapshot (Yahoo via yfinance).

ANALYST_VERSION v1. Fields (display-only — deliberately NOT fed into fusion):
  n_analysts          numberOfAnalystOpinions
  recommendation_*    recommendationKey (strong_buy..sell) + recommendationMean (1=strong buy)
  breakdown           current-month strongBuy/buy/hold/sell/strongSell counts
  target_mean/high/low/median   12-month price targets
  upside_pct          target_mean / ref_price - 1  (ref_price = options spot when given)
  forward_pe / trailing_pe

Caveats: Yahoo aggregates use its own taxonomy — counts may differ from
TIKR/FactSet/Bloomberg tallies; data is delayed; a mean target is not a
forecast and ratings are not trading signals.
"""
from __future__ import annotations

from datetime import datetime, timezone

import yfinance as yf

ANALYST_VERSION = "av1"

REC_ZH = {
    "strong_buy": "强买", "buy": "买", "hold": "持有",
    "underperform": "减持", "sell": "卖出", "none": "无共识",
}
BRK_KEYS = ("strongBuy", "buy", "hold", "sell", "strongSell")
BRK_ZH = {"strongBuy": "强买", "buy": "买", "hold": "持有", "sell": "卖出", "strongSell": "强卖"}


def _f(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def snapshot(ticker: str, spot: float | None = None) -> dict:
    """Best-effort fetch; never raises for missing fields (all None-able).
    spot: pass the options-layer spot so upside uses the same price the report shows."""
    t = yf.Ticker(ticker)
    out = {
        "ticker": ticker.upper(),
        "snapshot_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "yfinance",
        "analyst_version": ANALYST_VERSION,
        "n_analysts": None, "recommendation_key": None, "recommendation_mean": None,
        "breakdown": None, "target_mean": None, "target_high": None,
        "target_low": None, "target_median": None, "upside_pct": None,
        "forward_pe": None, "trailing_pe": None, "ref_price": None,
    }
    try:
        info = t.info or {}
    except Exception as e:
        out["error"] = f"info fetch failed: {e}"
        return out

    n = _f(info.get("numberOfAnalystOpinions"))
    out["n_analysts"] = int(n) if n else None
    out["recommendation_key"] = info.get("recommendationKey")
    rm = _f(info.get("recommendationMean"))
    out["recommendation_mean"] = round(rm, 2) if rm else None
    out["target_mean"] = _f(info.get("targetMeanPrice"))
    out["target_high"] = _f(info.get("targetHighPrice"))
    out["target_low"] = _f(info.get("targetLowPrice"))
    out["target_median"] = _f(info.get("targetMedianPrice"))
    out["forward_pe"] = _f(info.get("forwardPE"))
    out["trailing_pe"] = _f(info.get("trailingPE"))

    price = spot or _f(info.get("currentPrice")) or _f(info.get("regularMarketPrice"))
    out["ref_price"] = round(price, 2) if price else None
    if out["target_mean"] and price:
        out["upside_pct"] = round(out["target_mean"] / price - 1.0, 4)

    # monthly ratings breakdown, current bucket — best-effort, versions differ
    try:
        rec = getattr(t, "recommendations", None)
        if rec is None:
            rec = t.get_recommendations()
        if rec is not None and len(rec):
            row = rec.loc["0m"] if "0m" in getattr(rec, "index", []) else rec.iloc[0]
            brk = {k: int(row[k]) for k in BRK_KEYS if row.get(k) is not None}
            if brk:
                out["breakdown"] = brk
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import json
    print(json.dumps(snapshot(sys.argv[1] if len(sys.argv) > 1 else "NVDA"),
                     indent=1, ensure_ascii=False))
