# /// script
# requires-python = ">=3.10"
# dependencies = ["yfinance", "tzdata"]
# ///
"""Options Adapter: yfinance option-chain snapshot -> structured metrics.

OPTIONS_VERSION v2 (ov2). Metrics (each deliberately simple, assumptions documented):
  atm_iv      ATM implied vol, near expiry, strikes within +/-3% of spot
  rr25        25-delta risk reversal = IV(call 25d) - IV(put 25d), same expiry
  term_slope  ATM IV(next expiry) - ATM IV(near expiry), vol points (x100)
  pc_oi       put OI / call OI (whole chain, both expiries)
  pc_vol      put vol / call vol
  gex         naive dealer gamma exposure, calls(+)/puts(-), per-1%-move USD

ov2 adds (P0 data-quality upgrade):
  validate()        ticker pre-check — fail fast BEFORE any Jev/API spend
  market_session    pre_market | open | after_hours | closed (ET, weekends, no holidays)
  data_as_of        when the chain was actually priced: now-15min if open,
                    else last US cash close 16:00 ET (weekends -> Friday)
  age_minutes       now - data_as_of
  data_quality      HIGH (live, delayed) / MEDIUM (frozen <=20h) / LOW (stale >20h)
  substates         direction | volatility | positioning — explicit three-lane read;
                    direction uses EXACTLY the ov1 rule (rr25 |x|>2vpt + pc_oi 0.7/1.3),
                    IV/term/GEX never vote on direction

All bands (20h staleness, IV 0.25/0.60, term +/-3vpt) are absolute first guesses;
re-derive as percentiles once history accumulates.

Data quality: rows with IV<=0.01 or IV>=3 dropped (Yahoo interpolation garbage);
expiries with DTE<2 skipped (expiry-week distortion). yfinance is 15-min delayed
and US-session oriented — fine for daily regime snapshots, useless intraday.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

OPTIONS_VERSION = "ov2"
ET = ZoneInfo("America/New_York")
STALE_HOURS = 20.0  # frozen chain older than this -> LOW / stale -> fusion refuses


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(s: float, k: float, t: float, sigma: float) -> float | None:
    if t <= 0 or sigma <= 0 or k <= 0 or s <= 0:
        return None
    return (math.log(s / k) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))


def call_delta(s: float, k: float, t: float, sigma: float) -> float | None:
    d1 = _d1(s, k, t, sigma)
    return None if d1 is None else _ncdf(d1)


def call_gamma(s: float, k: float, t: float, sigma: float) -> float | None:
    d1 = _d1(s, k, t, sigma)
    if d1 is None:
        return None
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return phi / (s * sigma * math.sqrt(t))


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else max(f, 0.0)   # NaN or negative -> 0


def _clean(df) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        iv = _num(r.get("impliedVolatility"))
        if not (0.01 < iv < 3.0):
            continue
        rows.append({
            "strike": float(r["strike"]),
            "iv": iv,
            "oi": _num(r.get("openInterest")),
            "vol": _num(r.get("volume")),
        })
    return rows


def _dte(expiry: str) -> float:
    exp = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return max((exp - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.0)


def _atm_iv(rows: list[dict], spot: float) -> float | None:
    near = sorted(rows, key=lambda r: abs(r["strike"] / spot - 1.0))[:4]  # within closest 4 strikes
    near = [r for r in near if abs(r["strike"] / spot - 1.0) <= 0.03]
    if not near:
        return None
    return sum(r["iv"] for r in near) / len(near)


def _rr25(calls: list[dict], puts: list[dict], spot: float, t: float) -> float | None:
    best_c, best_cdiff = None, 9e9
    for r in calls:
        d = call_delta(spot, r["strike"], t, r["iv"])
        if d is None:
            continue
        diff = abs(d - 0.25)
        if diff < best_cdiff:
            best_c, best_cdiff = r, diff
    best_p, best_pdiff = None, 9e9
    for r in puts:
        d = call_delta(spot, r["strike"], t, r["iv"])  # N(d1); put delta = N(d1)-1
        if d is None:
            continue
        diff = abs((d - 1.0) + 0.25)  # |put_delta - (-0.25)|
        if diff < best_pdiff:
            best_p, best_pdiff = r, diff
    if not best_c or not best_p:
        return None
    return best_c["iv"] - best_p["iv"]


def _gex(calls: list[dict], puts: list[dict], spot: float, t: float) -> float | None:
    """Naive per-1% GEX in USD: sum(call OI*g) - sum(put OI*g), *100*spot*0.01.
    Assumes dealers long calls' gamma sign +, puts' -. No OI-direction corrections."""
    g_call = g_put = 0.0
    for r in calls:
        g = call_gamma(spot, r["strike"], t, r["iv"])
        if g is not None:
            g_call += g * r["oi"]
    for r in puts:
        g = call_gamma(spot, r["strike"], t, r["iv"])
        if g is not None:
            g_put += g * r["oi"]
    if g_call + g_put == 0:
        return None
    return (g_call - g_put) * 100.0 * spot * 0.01


# ---------------------------------------------------------------- session ---

def market_session(now_et: datetime) -> str:
    """US cash session state in ET. Holidays NOT handled (first guess)."""
    if now_et.weekday() >= 5:
        return "closed"
    t = now_et.time()
    if dtime(9, 30) <= t < dtime(16, 0):
        return "open"
    if dtime(4, 0) <= t < dtime(9, 30):
        return "pre_market"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "after_hours"
    return "closed"  # weekday overnight (20:00-24:00 / 00:00-04:00)


def _prev_trading_day(d) -> "datetime.date":
    # Monday -> Friday, else previous day (weekends only path here)
    return d - timedelta(days=3 if d.weekday() == 0 else 1)


def data_as_of(now_et: datetime, session: str) -> tuple[datetime, str]:
    """(when the chain was priced, status). open -> now-15min (delay); else last close."""
    if session == "open":
        return now_et - timedelta(minutes=15), "live_delayed"
    if session == "after_hours":
        return datetime.combine(now_et.date(), dtime(16, 0), tzinfo=ET), "frozen"
    if session == "pre_market":
        return datetime.combine(_prev_trading_day(now_et.date()), dtime(16, 0), tzinfo=ET), "frozen"
    # closed: weekday overnight vs weekend
    d = now_et.date()
    if d.weekday() == 5:            # Saturday
        d = d - timedelta(days=1)
    elif d.weekday() == 6:          # Sunday
        d = d - timedelta(days=2)
    elif now_et.time() < dtime(4, 0):  # weekday 00:00-04:00 -> previous close
        d = _prev_trading_day(d)
    return datetime.combine(d, dtime(16, 0), tzinfo=ET), "frozen"


def _data_quality(now_et: datetime, asof: datetime, status: str, flags: list[str]) -> dict:
    age_min = max((now_et - asof).total_seconds() / 60.0, 0.0)
    reasons = list(flags)
    if status == "live_delayed":
        level = "HIGH"
        reasons.append("15min_delayed")
    elif age_min <= STALE_HOURS * 60:
        level = "MEDIUM"
        reasons.append("frozen_offhours")
    else:
        level = "LOW"
        reasons.append(f"frozen_{age_min / 60:.0f}h_gt_{STALE_HOURS:.0f}h_STALE")
    return {
        "level": level,
        "status": status,
        "stale": level == "LOW",
        "data_as_of": asof.isoformat(timespec="minutes"),
        "age_minutes": round(age_min),
        "reasons": reasons,
    }


# --------------------------------------------------------------- substates ---

def _substates(o: dict, flags: list[str]) -> dict:
    """Three-lane structured read. Direction = EXACTLY the ov1 rule; IV/term/GEX
    never vote on direction (IV is magnitude, GEX is hedging structure)."""
    rr, pc = o.get("rr25_vpt"), o.get("pc_oi")
    ev: list[tuple[str, int]] = []
    if rr is not None and abs(rr) > 2.0:
        ev.append((f"rr25 {rr:+.1f}vpt", 1 if rr > 0 else -1))
    if pc is not None and pc < 0.7:
        ev.append((f"pc_oi {pc:.2f} call-heavy", 1))
    elif pc is not None and pc > 1.3:
        ev.append((f"pc_oi {pc:.2f} put-heavy", -1))
    s = sum(v for _, v in ev)
    direction = ("BULLISH" if s >= 2 else "LEAN_BULLISH" if s == 1 else
                 "LEAN_BEARISH" if s == -1 else "BEARISH" if s <= -2 else "NEUTRAL")

    iv, slope = o.get("atm_iv_near"), o.get("term_slope_vpt")
    iv_read = ("elevated" if (iv or 0) > 0.60 else "low" if iv and iv < 0.25 else "normal")
    term_read = ("front_loaded" if (slope or 0) > 3 else
                 "backwardated" if (slope or 0) < -3 else "flat")
    pos_read = ("call_heavy" if (pc is not None and pc < 0.7) else
                "put_heavy" if (pc is not None and pc > 1.3) else "balanced")
    gex = o.get("gex_usd_per_1pct")
    gex_read = "positive" if (gex or 0) > 0 else ("negative" if gex else None)
    return {
        "direction": {"state": direction, "evidence": [n for n, _ in ev]},
        "volatility": {"atm_iv": iv, "read": iv_read,
                       "term_slope_vpt": slope, "term": term_read},
        "positioning": {"pc_oi": pc, "pc_vol": o.get("pc_vol"),
                        "gex_sign": gex_read, "read": pos_read},
        "note": "direction from rr25+pc_oi only; IV/term/GEX informational",
    }


# ----------------------------------------------------------------- public ---

def validate(ticker: str) -> str:
    """Fail fast on bogus tickers BEFORE spending Jev/API budget.
    Resolves via Yahoo: needs a symbol AND a real price print."""
    t = yf.Ticker(ticker)
    info = {}
    try:
        info = t.info or {}
    except Exception as e:
        if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e) or "429" in str(e):
            raise RuntimeError(
                f"Yahoo rate-limited for '{ticker}' — data source unavailable, "
                "retry later or use cached snapshots") from e
        info = {}
    sym, px = info.get("symbol"), (info.get("currentPrice") or info.get("regularMarketPrice")
                                   or info.get("previousClose"))
    if not sym or px is None:
        raise RuntimeError(
            f"invalid/unknown ticker '{ticker}' (Yahoo: symbol={sym!r}, price={px!r}). "
            "Check the code — e.g. AXT Inc trades as AXTI, not AXT.")
    return str(sym)


def snapshot(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    spot = None
    try:
        spot = t.fast_info.get("lastPrice")
    except Exception:
        pass
    if not spot:
        try:
            spot = float(t.history(period="1d")["Close"].iloc[-1])
        except Exception:
            pass
    if not spot:
        raise RuntimeError(f"no spot price for {ticker}")

    expiries = [e for e in t.options if _dte(e) >= 2]
    if len(expiries) < 2:
        raise RuntimeError(f"not enough usable expiries for {ticker}")
    e1, e2 = expiries[0], expiries[1]
    t1, t2 = _dte(e1) / 365.0, _dte(e2) / 365.0

    ch1 = t.option_chain(e1)
    ch2 = t.option_chain(e2)
    c1, p1 = _clean(ch1.calls), _clean(ch1.puts)
    c2, p2 = _clean(ch2.calls), _clean(ch2.puts)
    flags = []
    if min(len(c1), len(p1)) < 5 or min(len(c2), len(p2)) < 5:
        flags.append("thin_chain")

    atm1 = _atm_iv(c1 + p1, spot)
    atm2 = _atm_iv(c2 + p2, spot)
    rr25 = _rr25(c1, p1, spot, t1)
    pc_oi = (sum(r["oi"] for r in p1 + p2) / sum(r["oi"] for r in c1 + c2)) if sum(r["oi"] for r in c1 + c2) else None
    pc_vol = (sum(r["vol"] for r in p1 + p2) / sum(r["vol"] for r in c1 + c2)) if sum(r["vol"] for r in c1 + c2) else None
    gex = _gex(c1, p1, spot, t1)

    now_et = datetime.now(ET)
    session = market_session(now_et)
    asof, status = data_as_of(now_et, session)

    out = {
        "ticker": ticker.upper(),
        "spot": round(float(spot), 2),
        "snapshot_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expiries_used": [e1, e2],
        "dte": [round(_dte(e1), 1), round(_dte(e2), 1)],
        "atm_iv_near": round(atm1, 4) if atm1 else None,
        "atm_iv_next": round(atm2, 4) if atm2 else None,
        "term_slope_vpt": round((atm2 - atm1) * 100, 1) if (atm1 and atm2) else None,
        "rr25_vpt": round(rr25 * 100, 1) if rr25 is not None else None,
        "pc_oi": round(pc_oi, 3) if pc_oi else None,
        "pc_vol": round(pc_vol, 3) if pc_vol else None,
        "gex_usd_per_1pct": round(gex) if gex else None,
        "quality_flags": flags,
        "options_version": OPTIONS_VERSION,
        # --- ov2 ---
        "market_session": session,
        "data_as_of": asof.isoformat(timespec="minutes"),
        "age_minutes": round(max((now_et - asof).total_seconds() / 60.0, 0.0)),
        "data_quality": _data_quality(now_et, asof, status, flags),
    }
    out["substates"] = _substates(out, flags)
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import json
    print(json.dumps(snapshot(sys.argv[1] if len(sys.argv) > 1 else "NVDA"),
                     indent=1, ensure_ascii=False))
