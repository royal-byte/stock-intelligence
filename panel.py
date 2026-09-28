# /// script
# requires-python = ">=3.10"
# dependencies = ["tzdata"]
# ///
"""Regime Panel: version-consistent time series over data/fusion/*.json.

The payoff mechanism for daily snapshots — without this, accumulated data has
no retrieval path. One row per (ticker, US/ET trading date), latest run wins.

  uv run panel.py                 # latest FUSION_VERSION panel + threshold stats
  uv run panel.py --version fv1   # look at a legacy panel
  uv run panel.py --ticker SNDK   # single-name series
  uv run panel.py --csv out.csv   # also write CSV (backtest-ready)

Also prints absolute-threshold calibration stats (net_index / bull_share /
rr25 / pc_oi / atm_iv distributions) so the hardcoded first-guess bands
(0.35 / 0.80 / ±2vpt / 0.7-1.3 / IV 0.25-0.60) can later be re-derived as
cross-sectional percentiles once enough fv2 rows exist.

Version discipline: rows are grouped by fusion_version; only the requested
version enters the panel (see SKILL.md / references/backtest.md).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "fusion"
ET = ZoneInfo("America/New_York")

COLS = ["et_date", "ticker", "net_index", "narrative", "options", "alignment",
        "crowding", "atm_iv", "rr25_vpt", "pc_oi", "term_slope_vpt",
        "dq_sent", "dq_opt", "same_session", "fused"]


def _et_date(iso: str | None):
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(ET).date()


def load_raw() -> list[dict]:
    out = []
    for f in sorted(DATA.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta, fus = d.get("meta", {}), d.get("fusion", {})
        if not fus.get("fusion_version"):
            continue
        n = d.get("narrative", {}) or {}
        o = d.get("options", {}) or {}
        dq = d.get("data_quality", {}) or {}
        out.append({
            "fusion_version": fus["fusion_version"],
            "run_at": meta.get("run_at", ""),
            "et_date": str(_et_date(meta.get("run_at")) or ""),
            "ticker": o.get("ticker") or meta.get("ticker") or "?",
            "net_index": n.get("net_index"),
            "narrative": fus.get("narrative_state"),
            "options": fus.get("options_state"),
            "alignment": fus.get("alignment"),
            "crowding": fus.get("crowding"),
            "atm_iv": o.get("atm_iv_near"),
            "rr25_vpt": o.get("rr25_vpt"),
            "pc_oi": o.get("pc_oi"),
            "term_slope_vpt": o.get("term_slope_vpt"),
            "dq_sent": (dq.get("sentiment") or {}).get("level"),
            "dq_opt": (dq.get("options") or {}).get("level"),
            "same_session": dq.get("narrative_options_same_session"),
            "fused": (fus.get("fusion_gate") or {}).get("fused", True),
            "_file": f.name,
        })
    return out


def dedupe(rows: list[dict]) -> list[dict]:
    """One row per (ticker, et_date) — latest run_at wins."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["ticker"], r["et_date"])
        if k not in best or r["run_at"] >= best[k]["run_at"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["ticker"], r["et_date"]))


def _fmt(v, spec="") -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return format(v, spec or ".3f")
    return str(v)


def print_table(rows: list[dict]) -> None:
    w = {c: max(len(c), *(len(_fmt(r.get(c))) for r in rows)) if rows else len(c) for c in COLS}
    print("  ".join(c.upper().ljust(w[c]) for c in COLS))
    for r in rows:
        print("  ".join(_fmt(r.get(c)).ljust(w[c]) for c in COLS))


def _dist(name: str, vals: list[float]) -> None:
    vals = sorted(v for v in vals if v is not None)
    if len(vals) < 2:
        print(f"  {name}: n={len(vals)} (insufficient for distribution)")
        return
    q = statistics.quantiles(vals, n=4)
    print(f"  {name}: n={len(vals)} min={min(vals):.3f} p25={q[0]:.3f} "
          f"med={statistics.median(vals):.3f} p75={q[2]:.3f} max={max(vals):.3f}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="fusion_version to panel (default: latest present)")
    ap.add_argument("--ticker", help="single-ticker series")
    ap.add_argument("--csv", help="also write CSV to this path")
    args = ap.parse_args()

    raw = load_raw()
    if not raw:
        print("no fusion rows found under data/fusion/")
        return
    counts: dict[str, int] = {}
    for r in raw:
        counts[r["fusion_version"]] = counts.get(r["fusion_version"], 0) + 1
    latest = sorted(counts)[-1]
    ver = args.version or latest
    rows = dedupe([r for r in raw if r["fusion_version"] == ver])
    if args.ticker:
        rows = [r for r in rows if r["ticker"] == args.ticker.upper()]

    print(f"━━ regime panel（fusion_version={ver}｜原始 {counts[ver]} 行 → 去重后 {len(rows)} 行｜"
          f"其他版本归存: { {k: v for k, v in counts.items() if k != ver} or '无'}）")
    print()
    print_table(rows)
    print()

    # regime / crowding tallies
    alg = {}
    for r in rows:
        alg[r["alignment"]] = alg.get(r["alignment"], 0) + 1
    crd = {}
    for r in rows:
        crd[str(r["crowding"])] = crd.get(str(r["crowding"]), 0) + 1
    print(f"alignment: {alg or '-'}")
    print(f"crowding:  {crd or '-'}")
    per = {}
    for r in rows:
        per.setdefault(r["ticker"], []).append(r["et_date"])
    span = ", ".join(f"{t}:{len(v)}d" for t, v in sorted(per.items()))
    print(f"coverage:  {span}")
    print()

    # threshold calibration stats (first-guess bands -> future percentiles)
    print("阈值校准参考（当前绝对阈值: |net|>0.35 极端 / share>0.80 单边 / "
          "rr25 ±2vpt / pc_oi 0.7-1.3 / IV 0.25-0.60）:")
    _dist("net_index", [r["net_index"] for r in rows])
    _dist("rr25_vpt", [float(r["rr25_vpt"]) for r in rows if r.get("rr25_vpt") is not None])
    _dist("pc_oi", [float(r["pc_oi"]) for r in rows if r.get("pc_oi") is not None])
    _dist("atm_iv", [float(r["atm_iv"]) for r in rows if r.get("atm_iv") is not None])
    _dist("term_slope", [float(r["term_slope_vpt"]) for r in rows if r.get("term_slope_vpt") is not None])

    if args.csv:
        import csv as _csv
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            wtr = _csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
            wtr.writeheader()
            wtr.writerows(rows)
        print(f"\nCSV 已写: {args.csv}")


if __name__ == "__main__":
    main()
