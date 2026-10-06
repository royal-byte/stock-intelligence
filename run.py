# /// script
# requires-python = ">=3.10"
# dependencies = ["yfinance", "tzdata"]
# ///
"""stock-intelligence unified entry: narrative (Jev) x options pricing -> regime.

Modes:
  --mode sentiment   deep X/Jev run only (fresh opencli fetch; slow, ~1-2 min)
  --mode options     options snapshot only
  --mode full        default: reuse fresh (<24h) v2 narrative snapshot if usable,
                     else run one; then options + fusion + combined report
  --mode all         full pipeline over watchlist.json (no fresh sentiment fetch)

Schema 1.2 (P0 data-quality upgrade):
  - ticker pre-validation BEFORE any Jev/API spend (AXT vs AXTI class of error)
  - data_quality block: per-source level + observed_at/data_as_of/age_minutes,
    US market-session awareness (frozen after-hours options), narrative
    confidence gating (<10 unique authors -> LOW), narrative-vs-options
    same-trading-session check
  - STALE options (frozen >20h) -> fusion refuses (NOT_FUSED, no crowding)
  - report gains block 0 (snapshot) and block 6 (data quality)

Key discipline: information fuses LATE and as STATES (alignment/divergence/
crowding), never as a single score. Descriptive only, no trading signals.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "options"))
sys.path.insert(0, str(ROOT))

import analysts  # noqa: E402
import fusion  # noqa: E402
import options  # noqa: E402

SCHEMA_VERSION = "1.2"
SENTIMENT_DIR = ROOT / "sentiment"
DATA = ROOT / "data" / "fusion"
FRESH_HOURS = 24
ET = ZoneInfo("America/New_York")


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    print(r.stdout, end="")
    if r.returncode != 0:
        print((r.stderr or "")[-800:], file=sys.stderr)
        raise RuntimeError(f"subprocess failed: {cmd[:3]}")


def fresh_narrative(ticker: str) -> dict | None:
    """Usable v2 narrative snapshot newer than FRESH_HOURS (opencli preferred)."""
    snap = fusion.find_latest_narrative(ticker)
    if not snap:
        return None
    run_at = snap["meta"].get("run_at", "")
    try:
        t = dt.datetime.fromisoformat(run_at)
        if dt.datetime.now() - t > dt.timedelta(hours=FRESH_HOURS):
            return None
    except ValueError:
        return None
    return snap


def run_sentiment(ticker: str, n: int) -> None:
    _run(["uv", "run", str(SENTIMENT_DIR / "run.py"),
          "--ticker", ticker, "--source", "opencli", "--n", str(n),
          "--product", "live", "--x-query", "min_faves:3 -filter:replies"])


# ------------------------------------------------------------ data quality ---

def _sentiment_dq(agg: dict, run_at: str | None) -> dict:
    """HIGH/MEDIUM/LOW by sample size (first-guess bands: 20帖+10作者 / 10+5)."""
    n_eff = agg.get("n_effective") or 0
    uniq = agg.get("unique_effective_authors") or 0
    if not agg:
        return {"level": "MISSING", "n_effective": 0, "unique_authors": 0, "age_hours": None}
    level = ("HIGH" if n_eff >= 20 and uniq >= 10 else
             "MEDIUM" if n_eff >= 10 and uniq >= 5 else "LOW")
    age = None
    if run_at:
        try:
            age = round((dt.datetime.now() - dt.datetime.fromisoformat(run_at)).total_seconds() / 3600, 1)
        except ValueError:
            pass
    return {"level": level, "n_effective": n_eff, "unique_authors": uniq, "age_hours": age}


def _analyst_dq(ana: dict) -> dict:
    n = ana.get("n_analysts") or 0
    if ana.get("error"):
        level = "MISSING"
    else:
        level = "HIGH" if n >= 10 else ("MEDIUM" if n >= 3 else "LOW")
    return {"level": level, "coverage": n or None, "source": "yfinance_aggregation"}


def _et_date(iso: str | None):
    """US/Eastern trading date of a timestamp (naive -> local machine tz first)."""
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(ET).date()


# ------------------------------------------------------------------ pipeline ---

def one(ticker: str, mode: str, n: int, fresh: bool) -> dict:
    ticker = ticker.upper()
    if mode == "sentiment":
        run_sentiment(ticker, n)
        return {}

    if mode in ("full", "options"):
        options.validate(ticker)  # fail fast BEFORE any Jev/API spend

    opts = options.snapshot(ticker)
    try:
        ana = analysts.snapshot(ticker, spot=opts.get("spot"))
    except Exception as e:
        ana = {"ticker": ticker, "error": str(e)}
    narrative = fresh_narrative(ticker)
    if narrative is None and mode == "full" and not fresh:
        print(f"[{ticker}] no fresh (<{FRESH_HOURS}h) v2 narrative snapshot — running one now")
        try:
            run_sentiment(ticker, n)
            narrative = fusion.find_latest_narrative(ticker)
        except Exception as e:
            print(f"[{ticker}] sentiment fetch failed ({e}); continuing options-only")
    elif fresh:
        narrative = fusion.find_latest_narrative(ticker)

    f = fusion.fuse(ticker, opts, narrative)
    agg = (narrative or {}).get("aggregate", {})
    narr_run_at = (narrative or {}).get("meta", {}).get("run_at")

    odq = opts.get("data_quality", {})
    sdq = _sentiment_dq(agg, narr_run_at)
    adq = _analyst_dq(ana)
    same_session = None
    if narr_run_at and odq.get("data_as_of"):
        a, b = _et_date(odq["data_as_of"]), _et_date(narr_run_at)
        if a and b:
            same_session = (a == b)

    gates = list(f.get("fusion_gate", {}).get("reasons", []))
    if same_session is False:
        gates.append(f"cross_session: narrative ET date {_et_date(narr_run_at)} != "
                     f"options ET date {_et_date(odq['data_as_of'])}（定价未覆盖叙事时段）")
    if sdq["level"] == "LOW":
        gates.append(f"sentiment sample thin: {sdq['n_effective']} posts / "
                     f"{sdq['unique_authors']} unique authors (<10/5) — confidence LOW")

    row = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "options_version": opts["options_version"],
            "fusion_version": f["fusion_version"],
            "narrative_source": narrative["file"] if narrative else None,
            "narrative_prompt_version": (narrative or {}).get("meta", {}).get("prompt_version"),
            "narrative_run_at": narr_run_at,
            "mode": mode,
            "run_at": dt.datetime.now().isoformat(timespec="seconds"),
            "us_market_session": opts.get("market_session"),
        },
        "options": opts,
        "analyst": ana,
        "narrative": {
            "net_index": agg.get("net_index"), "verdict": agg.get("verdict"),
            "polarization": agg.get("polarization"),
            "polarization_level": agg.get("polarization_level"),
            "bull_share": agg.get("bull_share"), "bear_share": agg.get("bear_share"),
            "bullish_weight": agg.get("bullish_weight"), "bearish_weight": agg.get("bearish_weight"),
            "bull_bear_ratio": agg.get("bull_bear_ratio"),
            "extreme_consensus": agg.get("extreme_consensus"),
            "n_effective": agg.get("n_effective"),
            "unique_effective_authors": agg.get("unique_effective_authors"),
        } if narrative else {},
        "data_quality": {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "sentiment": sdq,
            "options": {
                "level": odq.get("level"), "status": odq.get("status"),
                "data_as_of": odq.get("data_as_of"), "age_minutes": odq.get("age_minutes"),
                "stale": odq.get("stale", False),
                "chain_flags": opts.get("quality_flags", []),
            },
            "analyst": adq,
            "narrative_options_same_session": same_session,
            "gates": gates,
        },
        "fusion": f,
    }
    DATA.mkdir(exist_ok=True)
    out = DATA / f"{ticker.lower()}_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(row))
    print(f"已存: {out}\n")
    return row


def format_report(r: dict) -> str:
    t = r["options"]["ticker"]
    o, n, f = r["options"], r["narrative"], r["fusion"]
    a = r.get("analyst") or {}
    dq = r.get("data_quality") or {}
    odq, sdq, adq = dq.get("options", {}), dq.get("sentiment", {}), dq.get("analyst", {})
    L = [f"━━ {t} stock-intelligence（{SCHEMA_VERSION}/{o['options_version']}/"
         f"{(a or {}).get('analyst_version', '-')}/{f['fusion_version']}，mode={r['meta']['mode']}）"]
    # ⓪ snapshot line
    sess = o.get("market_session", "?")
    status, asof = odq.get("status"), o.get("data_as_of", "")
    age_h = (odq.get("age_minutes") or 0) / 60.0
    asof_txt = asof.replace("T", " ").replace("-04:00", " ET").replace("-05:00", " ET") if asof else "?"
    L.append(f"⓪ 快照｜美股时段 {sess}｜期权 {status}"
             + (f"（截至 {asof_txt}，{age_h:.1f}h 前）" if status == "frozen" else "（15min 延迟）"))
    # ① narrative
    L.append(f"① 叙事层 @ {r['meta'].get('narrative_run_at') or '缺失'}")
    if n:
        rr, bw, sw = n.get("bull_bear_ratio"), n.get("bullish_weight"), n.get("bearish_weight")
        ratio_txt = (f"{rr:.1f}:1（{bw:.2f} vs {sw:.2f}）" if rr is not None
                     else (f"全多（{bw:.2f} vs 0.00）" if bw and not sw else "n/a"))
        L.append(f"   净指数 {n['net_index']:+.3f}（{n['verdict']}）｜极化 {n['polarization_level']}"
                 f"({n['polarization']:.2f})｜多{0 if n['bull_share'] is None else n['bull_share']:.0%}"
                 f"/空{0 if n['bear_share'] is None else n['bear_share']:.0%}"
                 f"｜有效帖 {n['n_effective']}/独立作者 {n['unique_effective_authors']}"
                 f"｜多空权重比 {ratio_txt}")
    else:
        L.append("   无可用快照")
    # ② options
    L.append("② 期权层（yfinance 延迟快照）")
    _iv = f"{o['atm_iv_near']:.1%}" if o.get('atm_iv_near') is not None else 'n/a'
    _ts = f"{o['term_slope_vpt']:+.1f}" if o.get('term_slope_vpt') is not None else 'n/a'
    _rr = f"{o['rr25_vpt']:+.1f}" if o.get('rr25_vpt') is not None else 'n/a'
    _gex = f"{o['gex_usd_per_1pct']:+,.0f}" if o.get('gex_usd_per_1pct') is not None else 'n/a'
    L.append(f"   spot {o.get('spot', 'n/a')}｜ATM IV {_iv}（期限斜率 {_ts}vpt"
             f"{'（驼峰=近期事件预期）' if (o.get('term_slope_vpt') or 0) > 3 else ''}）")
    L.append(f"   25Δ RR {_rr}vpt｜PC OI {o.get('pc_oi', 'n/a')}｜PC vol {o.get('pc_vol', 'n/a')}"
             f"｜GEX/1% {_gex}USD")
    sub = o.get("substates") or {}
    if sub:
        d, v, p = sub.get("direction", {}), sub.get("volatility", {}), sub.get("positioning", {})
        L.append(f"   子状态: 方向 {d.get('state')}（{'; '.join(d.get('evidence', [])) or '无'}）"
                 f"｜波动 {v.get('read')}/{v.get('term')}｜仓位 {p.get('read')}"
                 f"（GEX {p.get('gex_sign') or 'n/a'}）")
    # ③ analysts
    L.append("③ 卖方共识与估值（yfinance 汇总·独立数据点，不参与融合）")
    if a and not a.get("error") and any(a.get(k) for k in ("n_analysts", "target_mean", "recommendation_key", "forward_pe")):
        parts = []
        if a.get("n_analysts"):
            parts.append(f"{a['n_analysts']} 家覆盖")
        if a.get("recommendation_key"):
            s = f"共识 {analysts.REC_ZH.get(a['recommendation_key'], a['recommendation_key'])}"
            if a.get("recommendation_mean"):
                s += f"（{a['recommendation_mean']:.1f}/5，1=强买）"
            parts.append(s)
        if a.get("breakdown"):
            parts.append("/".join(f"{analysts.BRK_ZH[k]}{v}" for k, v in a["breakdown"].items() if v))
        if a.get("target_mean"):
            s = f"目标均值 ${a['target_mean']:,.0f}"
            if a.get("upside_pct") is not None:
                s += f"（较现价 {a['upside_pct']:+.0%}）"
            if a.get("target_low") and a.get("target_high"):
                s += f"｜区间 ${a['target_low']:,.0f}–${a['target_high']:,.0f}"
            parts.append(s)
        pes = []
        if a.get("forward_pe"):
            pes.append(f"前瞻 PE {a['forward_pe']:.1f}x")
        if a.get("trailing_pe"):
            pes.append(f"静态 PE {a['trailing_pe']:.1f}x")
        if pes:
            parts.append("｜".join(pes))
        L.append("   " + " ｜ ".join(parts))
    else:
        L.append("   无卖方覆盖数据（小票/新票或 Yahoo 未收录）")
    # ④ fusion
    L.append("④ 融合（状态融合，非单一分数）")
    L.append(f"   叙事 {f['narrative_state']}｜定价 {f['options_state']}"
             f"（证据: {'; '.join(f['options_evidence']) or '无'}）")
    crowd_txt = f['crowding'] if f['crowding'] else "—（未融合）"
    L.append(f"   对齐 {f['alignment']}｜拥挤 {crowd_txt}")
    # ⑤ regime
    L.append(f"⑤ Regime: {f['regime_label']}")
    if f["crowding"] == "HIGH":
        L.append("   ⚠ 拥挤 HIGH：叙事极端且定价同向——预期可能已充分定价，防证伪风险。")
    # ⑥ data quality
    s_age = f"{sdq.get('age_hours')}h" if sdq.get("age_hours") is not None else "?"
    L.append(f"⑥ 数据质量｜叙事 {sdq.get('level')}（{sdq.get('n_effective')}帖/"
             f"{sdq.get('unique_authors')}作者，{s_age}前）"
             f"｜期权 {odq.get('level')}（{odq.get('status')}）"
             f"｜卖方 {adq.get('level')}（{adq.get('coverage') or 0}家，Yahoo 口径）")
    if dq.get("narrative_options_same_session") is False:
        L.append("   ⚠ 跨时段：叙事快照与期权数据不属于同一美股交易日")
    for g in dq.get("gates", []):
        L.append(f"   ⚠ {g}")
    L.append("   边界: 描述性状态，非买卖信号；期权/卖方共识为延迟免费数据，盘外时段为期权链"
             "冻结值（上一收盘口径）（Yahoo 汇总口径，与 TIKR/FactSet 等统计可能不一致）；"
             "评级与目标价非买卖信号；GEX 为朴素口径。")
    return "\n".join(L)


def load_watchlist() -> tuple[list[str], dict[str, list[str]]]:
    wl = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    if isinstance(wl, dict):
        return wl.get("tickers", []), wl.get("clusters") or {}
    return wl, {}


def _near_high(agg: dict) -> bool:
    """临界拥挤：差一步触发 narrative_extreme（0.30≤|net|<0.35 或 0.75≤单边<0.80）。"""
    net = abs(agg.get("net_index") or 0)
    return (0.30 <= net < 0.35
            or 0.75 <= (agg.get("bull_share") or 0) < 0.80
            or 0.75 <= (agg.get("bear_share") or 0) < 0.80)


def latest_fusion_row(ticker: str) -> dict | None:
    """Newest stored fusion snapshot for a ticker (any schema version)."""
    for f in sorted(DATA.glob(f"{ticker.lower()}_*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (d.get("fusion") or {}).get("fusion_version"):
            return d
    return None


def portfolio_run(n: int) -> dict:
    """Cross-sectional portfolio dashboard: options+fusion per watchlist name
    (no fresh sentiment fetch — reuses latest snapshots, flags staleness).
    If Yahoo is unavailable (rate-limit/outage), falls back to the newest stored
    fusion row per ticker and marks the report as cached."""
    tickers, clusters = load_watchlist()
    now = dt.datetime.now()
    entries = []
    for t in tickers:
        try:
            options.validate(t)
            opts = options.snapshot(t)
            cached = None
        except Exception as e:
            cached = latest_fusion_row(t)
            if not cached:
                entries.append({"ticker": t.upper(), "error": str(e)})
                continue
            opts = cached.get("options", {})
        narr = fresh_narrative(t) or fusion.find_latest_narrative(t)
        f = cached["fusion"] if cached else fusion.fuse(t, opts, narr)
        agg = (narr or {}).get("aggregate", {}) if not cached else (cached.get("narrative") or {})
        if cached:  # prefer the row's own narrative block (matches its options vintage)
            agg = cached.get("narrative") or agg
        narr_run_at = (narr or {}).get("meta", {}).get("run_at") if not cached else cached.get("meta", {}).get("narrative_run_at")
        is_fresh = False
        if narr_run_at:
            try:
                is_fresh = (now - dt.datetime.fromisoformat(narr_run_at)) <= dt.timedelta(hours=FRESH_HOURS)
            except ValueError:
                pass
        entries.append({
            "ticker": t.upper(), "opts": opts, "fusion": f, "agg": agg,
            "narrative_fresh": is_fresh, "narrative_run_at": narr_run_at,
            "cached_from": (cached or {}).get("meta", {}).get("run_at") if cached else None,
        })

    ok = [e for e in entries if "error" not in e]
    report = _portfolio_report(ok, entries, clusters)
    out_dir = ROOT / "data" / "portfolio"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"portfolio_{now:%Y%m%d_%H%M%S}.json"
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "tickers": tickers,
        "clusters": clusters,
        "entries": [{
            "ticker": e["ticker"],
            "net_index": e["agg"].get("net_index"),
            "bull_share": e["agg"].get("bull_share"),
            "narrative_state": e["fusion"]["narrative_state"],
            "options_state": e["fusion"]["options_state"],
            "alignment": e["fusion"]["alignment"],
            "crowding": e["fusion"]["crowding"],
            "near_high": _near_high(e["agg"]),
            "atm_iv": e["opts"].get("atm_iv_near"),
            "rr25_vpt": e["opts"].get("rr25_vpt"),
            "term_slope_vpt": e["opts"].get("term_slope_vpt"),
            "narrative_fresh": e["narrative_fresh"],
            "options_dq": (e["opts"].get("data_quality") or {}).get("level"),
        } for e in ok],
        "errors": [e for e in entries if "error" in e],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    print(f"已存: {out}\n")
    return payload


def _portfolio_report(ok: list[dict], entries: list[dict], clusters: dict[str, list[str]]) -> str:
    now = dt.datetime.now()
    L = [f"━━ Portfolio stock-intelligence（{len(ok)}/{len(entries)} 只｜fv2｜{now:%Y-%m-%d %H:%M}）"]
    # ⓪ data
    cached_n = sum(1 for e in ok if e.get("cached_from"))
    fresh_n = sum(1 for e in ok if e["narrative_fresh"])
    odqs = [(e["opts"].get("data_quality") or {}) for e in ok]
    opt_levels = [d.get("level") for d in odqs if d.get("level")]
    sess = ok[0]["opts"].get("market_session") if ok else "?"
    stale_n = sum(1 for d in odqs if d.get("stale"))
    L.append(f"⓪ 数据｜叙事新鲜(<{FRESH_HOURS}h) {fresh_n}/{len(ok)}"
             f"｜美股时段 {sess}｜期权 {dict((l, opt_levels.count(l)) for l in set(opt_levels))}"
             + (f"｜⚠ STALE {stale_n}" if stale_n else "")
             + (f"｜⚠ Yahoo 不可用，{cached_n} 只回退缓存快照（期权/融合为存量口径）" if cached_n else ""))
    # ① regime
    alg = {}
    for e in ok:
        alg[e["fusion"]["alignment"]] = alg.get(e["fusion"]["alignment"], 0) + 1
    L.append(f"① Regime｜" + "｜".join(f"{k} {v}" for k, v in sorted(alg.items())))
    # ② crowding
    high = [e for e in ok if e["fusion"]["crowding"] == "HIGH"]
    near = [e for e in ok if _near_high(e["agg"]) and e["fusion"]["crowding"] != "HIGH"]
    med = sum(1 for e in ok if e["fusion"]["crowding"] == "MEDIUM")
    hi_txt = ", ".join(f"{e['ticker']}({(e['agg'].get('net_index') or 0):+.2f})" for e in
                       sorted(high, key=lambda x: -(x["agg"].get("net_index") or 0))) or "无"
    L.append(f"② 拥挤｜HIGH {len(high)}（{hi_txt}）｜临界 {len(near)}（"
             f"{', '.join(e['ticker'] for e in near) or '无'}）｜MEDIUM {med}"
             f"｜HIGH+临界占比 {len(high) + len(near)}/{len(ok)}")
    # ③ clusters
    L.append("③ 相关簇（同簇≈同一笔交易）")
    assigned = {t for ts in clusters.values() for t in ts}
    for name, ts in clusters.items():
        members = [e for e in ok if e["ticker"] in ts]
        if not members:
            continue
        hi = [e["ticker"] for e in members if e["fusion"]["crowding"] == "HIGH"]
        nets = [e["agg"].get("net_index") for e in members if e["agg"].get("net_index") is not None]
        mean_net = sum(nets) / len(nets) if nets else None
        tags = " ".join(f"{e['ticker']}[{e['fusion']['crowding'] or '-'}]" for e in members)
        L.append(f"   {name}({len(members)}): {tags}")
        if hi or mean_net is not None:
            L.append(f"      → 簇内 HIGH {len(hi)}/{len(members)}"
                     + (f"｜簇均值净指数 {mean_net:+.3f}" if mean_net is not None else ""))
    unassigned = [e for e in ok if e["ticker"] not in assigned]
    if unassigned:
        L.append("   未分组: " + " ".join(e["ticker"] for e in unassigned))
    # ④ temperature
    ranked = sorted((e for e in ok if e["agg"].get("net_index") is not None),
                    key=lambda e: -(e["agg"]["net_index"]))
    L.append("④ 温度榜（净指数）: " + " ▸ ".join(
        f"{e['ticker']} {e['agg']['net_index']:+.2f}" for e in ranked))
    # ⑤ event humps
    humps = [(e["ticker"], e["opts"].get("term_slope_vpt")) for e in ok
             if (e["opts"].get("term_slope_vpt") or 0) > 3]
    L.append("⑤ IV 近端驼峰（事件预期）: "
             + (", ".join(f"{t}({v:+.1f}vpt)" for t, v in humps) or "无"))
    errs = [e for e in entries if "error" in e]
    if errs:
        L.append("⑥ 失败: " + "; ".join(f"{e['ticker']}: {e['error'][:80]}" for e in errs))
    L.append("   边界: 描述性组合状态，非调仓信号；拥挤/临界是叙事×定价标记非反转规则；"
             "期权为延迟冻结口径；簇标注基于主观产业链归类（watchlist.json 可改）。")
    return "\n".join(L)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker")
    ap.add_argument("--mode", choices=["sentiment", "options", "full", "all", "portfolio"], default="full")
    ap.add_argument("--n", type=int, default=50, help="sentiment posts to fetch when running fresh")
    ap.add_argument("--fresh", action="store_true", help="force reuse of any narrative snapshot")
    args = ap.parse_args()

    if args.mode == "all":
        wl, _ = load_watchlist()
        print(f"watchlist: {wl}")
        for t in wl:
            try:
                one(t, "options", args.n, fresh=True)
            except Exception as e:
                print(f"━━ {t}: FAILED — {e}\n")
    elif args.mode == "portfolio":
        portfolio_run(args.n)
    elif args.ticker:
        one(args.ticker, args.mode, args.n, fresh=False)
    else:
        ap.error("need --ticker XXX (or --mode all)")


if __name__ == "__main__":
    main()
