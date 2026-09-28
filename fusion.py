"""Fusion Engine: x-stock-sentiment narrative snapshot x options metrics -> regime.

FUSION_VERSION v2. All rules are deliberately explicit and interpretable —
no ML, no hidden weights. Thresholds are absolute first guesses; once enough
history accumulates they must be re-derived as cross-sectional/time percentiles
(see references/backtest.md).

V2 rule set (fv2 — P0 data-quality upgrade; fv1 rows are NOT panel-comparable):
  narrative_state: net_index > +0.15 BULLISH / < -0.15 BEARISH / else NEUTRAL
                   (requires n_effective >= 5, else INSUFFICIENT)
  options_state:   ov2 -> substates.direction (rr25 |x|>2vpt + pc_oi <0.7/>1.3;
                   2 agreeing -> that direction; 1 -> LEAN; 0 -> NEUTRAL).
                   IV/term/GEX never vote on direction. Legacy ov1 dicts
                   (no substates) fall back to the same rule computed here.
  STALE GATE:      options.data_quality.stale (frozen > 20h) -> options_state
                   = STALE, alignment = NOT_FUSED, crowding = None. Better no
                   regime than a regime built on a frozen chain (a false
                   "divergence" from stale pricing is worse than no answer).
  alignment:       same sign -> ALIGNED; opposite -> DIVERGENT; either NEUTRAL -> MIXED
  crowding:        narrative extreme AND options same-direction -> HIGH
                  narrative extreme, options neutral/lean -> MEDIUM, else LOW

Output keeps the four independent fields (narrative_state / options_state /
alignment / crowding — with relationship/pricing_direction aliases) plus
fusion_gate. regime_label is display-only prose; the fields are the data.
Divergence is a RESEARCH flag ("why do the two markets disagree?"), never a
trade signal. Everything here is descriptive regime, not advice.
"""
from __future__ import annotations

import json
from pathlib import Path

FUSION_VERSION = "fv2"
NARRATIVE_DIR = Path(__file__).parent / "data" / "sentiment"
NEEDED_PROMPT_VERSION = "v2"


def find_latest_narrative(ticker: str) -> dict | None:
    """Newest usable x-stock-sentiment snapshot (prompt_version v2) for ticker.
    Prefers real-X (opencli) snapshots over paste smoke-test ones."""
    files = sorted(NARRATIVE_DIR.glob(f"{ticker.lower()}_*.json"))
    fallback = None
    for f in reversed(files):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = d.get("meta", {})
        if meta.get("prompt_version") != NEEDED_PROMPT_VERSION:
            continue
        agg = d.get("aggregate", {})
        if agg.get("n_effective", 0) < 5:
            continue
        snap = {"file": f.name, "meta": meta, "aggregate": agg}
        if meta.get("run_args", {}).get("source") == "opencli":
            return snap
        if fallback is None:
            fallback = snap
    return fallback


def narrative_state(agg: dict) -> str:
    if agg.get("n_effective", 0) < 5:
        return "INSUFFICIENT"
    net = agg.get("net_index", 0.0)
    if net > 0.15:
        return "BULLISH"
    if net < -0.15:
        return "BEARISH"
    return "NEUTRAL"


def narrative_extreme(agg: dict) -> bool:
    return (abs(agg.get("net_index", 0)) > 0.35
            or agg.get("bull_share", 0) > 0.80
            or agg.get("bear_share", 0) > 0.80)


def options_state(opts: dict) -> tuple[str, list[str]]:
    """Direction read from pricing/positioning. Returns (state, evidence).
    ov2 snapshots carry substates.direction (same rule, computed at the source);
    legacy ov1 dicts fall through to the identical rule here."""
    sub = ((opts.get("substates") or {}).get("direction") or {})
    if sub:
        return sub.get("state", "NEUTRAL"), list(sub.get("evidence", []))
    ev: list[tuple[str, int]] = []
    rr = opts.get("rr25_vpt")
    if rr is not None and abs(rr) > 2.0:
        ev.append(("rr25", 1 if rr > 0 else -1))
    pc = opts.get("pc_oi")
    if pc is not None and pc < 0.7:
        ev.append(("pc_oi<0.7 (call-heavy OI)", 1))
    elif pc is not None and pc > 1.3:
        ev.append(("pc_oi>1.3 (put-heavy OI)", -1))
    s = sum(v for _, v in ev)
    if s >= 2:
        state = "BULLISH"
    elif s == 1:
        state = "LEAN_BULLISH"
    elif s == -1:
        state = "LEAN_BEARISH"
    elif s <= -2:
        state = "BEARISH"
    else:
        state = "NEUTRAL"
    return state, [name for name, _ in ev]


def fuse(ticker: str, opts: dict, narrative: dict | None) -> dict:
    agg = (narrative or {}).get("aggregate", {})
    n_state = narrative_state(agg) if agg else "MISSING"
    dq = opts.get("data_quality") or {}
    stale = bool(dq.get("stale"))
    gate_reasons: list[str] = []

    if stale:
        o_state, o_ev = "STALE", []
        gate_reasons.append(
            f"options stale: chain frozen {dq.get('age_minutes')}min ago "
            f"(as_of {dq.get('data_as_of')}) — refusing to fuse")
    else:
        o_state, o_ev = options_state(opts)
    bull = {"BULLISH", "LEAN_BULLISH"}
    bear = {"BEARISH", "LEAN_BEARISH"}

    if stale:
        alignment, crowding = "NOT_FUSED", None
    elif n_state in ("MISSING", "INSUFFICIENT") or o_state == "NEUTRAL":
        alignment = "MIXED"
    elif (n_state in bull and o_state in bull) or (n_state in bear and o_state in bear):
        alignment = "ALIGNED"
    elif (n_state in bull and o_state in bear) or (n_state in bear and o_state in bull):
        alignment = "DIVERGENT"
    else:
        alignment = "MIXED"

    extreme = bool(agg) and narrative_extreme(agg)
    if stale:
        pass
    elif extreme and ((n_state in bull and o_state in bull) or (n_state in bear and o_state in bear)):
        crowding = "HIGH"
    elif extreme:
        crowding = "MEDIUM"
    else:
        crowding = "LOW"

    labels = {
        ("ALIGNED", "BULLISH"): "看涨叙事 + 期权定价偏上行（对齐：共识已被付钱定价，查拥挤）",
        ("ALIGNED", "BEARISH"): "看空叙事 + 期权防御姿态（对齐：空头共识，查拥挤）",
        ("DIVERGENT", "BULLISH"): "叙事偏多但期权谨慎（背离：散户叙事 vs 定价分歧，研究为什么）",
        ("DIVERGENT", "BEARISH"): "叙事偏空但期权偏上行（背离：恐慌叙事 vs 定价坚挺，研究为什么）",
        ("MIXED", "BULLISH"): "叙事或定价一侧中性（信号弱，等待）",
        ("MIXED", "BEARISH"): "叙事或定价一侧中性（信号弱，等待）",
        ("MIXED", "NEUTRAL"): "两侧均中性（无状态）",
        ("MIXED", "MISSING"): "舆情快照缺失——先跑 x-stock-sentiment",
        ("MIXED", "INSUFFICIENT"): "舆情有效样本不足",
    }
    n_key = "MISSING" if n_state == "MISSING" else ("NEUTRAL" if n_state == "NEUTRAL" else
                                                    ("BULLISH" if n_state in bull else "BEARISH"))
    if stale:
        label = (f"期权数据过期（冻结 {dq.get('age_minutes')} 分钟前）——拒绝融合："
                 "防止把陈旧定价误读为对齐/背离。请在美股开盘时段重跑。")
    elif alignment == "MIXED":
        label = labels[("MIXED", "NEUTRAL" if n_key == "NEUTRAL" else n_key)]
    else:
        label = labels.get((alignment, n_key if n_key != "NEUTRAL" else "NEUTRAL"), "")

    return {
        "narrative_state": n_state,
        "options_state": o_state,
        "options_evidence": o_ev,
        "alignment": alignment,
        "relationship": alignment,            # explicit alias (structured-field discipline)
        "pricing_direction": o_state,         # explicit alias
        "crowding": crowding,
        "fusion_gate": {"fused": not stale, "reasons": gate_reasons},
        "regime_label": label,
        "fusion_version": FUSION_VERSION,
    }
