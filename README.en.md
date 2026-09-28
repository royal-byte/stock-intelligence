# stock-intelligence

[中文](README.md) | **English**

> X sentiment × options pricing → four-quadrant Regime. A US market state-research skill for coding agents.

**Outputs are always descriptive market states — not trading signals, not investment advice.**

## What it is

One skill, three modules, **late fusion of states** (never squashes all indicators into a single score):

| Layer | Answers | How |
|---|---|---|
| ① Narrative `sentiment/` | What is the market saying | Real X posts → per-post judging by Jev (stance / author quality / shilling detection / polarization / event extraction) → net bullish index + event panel |
| ② Options `options/` | What is the market paying for | yfinance chain snapshot → ATM IV / 25Δ RR / term slope / PC OI / naive GEX → direction / volatility / positioning sub-states |
| ③ Fusion `fusion.py` | Do the two markets agree | Four state fields (narrative_state / options_state / alignment / crowding) + data-quality gates → four-quadrant Regime |

Plus **sell-side consensus** (`analysts.py`, yfinance ratings / price targets / PE) shown as a standalone data point — it never enters the fusion.

## Decision flow

```mermaid
flowchart TB
    U["User asks: how is this stock doing?"] --> R{"Mode routing"}

    R -- "sentiment / mood only" --> MS["--mode sentiment"]
    R -- "options / pricing only" --> MO["--mode options"]
    R -- "full research (default)" --> MF["--mode full"]
    R -- "portfolio view" --> MP["--mode portfolio<br/>reuses latest snapshots, no sentiment re-run"]
    R -- "whole watchlist" --> MA["--mode all<br/>loops full per ticker"]
    R -- "time series / panel" --> MPA["panel.py<br/>version-consistent dedup + calibration stats"]

    MF --> P1{"Ticker precheck<br/>parseable on Yahoo?"}
    P1 -- "no" --> X1["Fail fast<br/>skip the pipeline"]

    P1 -- "yes" --> P2{"v2 sentiment snapshot<br/>younger than 24h?"}
    P2 -- "yes" --> REUSE["Reuse snapshot<br/>saves 1-2 min + API cost"]
    P2 -- "no / stale" --> N1

    subgraph L1["① Narrative layer · what the market is saying"]
        N1["opencli fetches real X posts"] --> N2["Jev per-post judging<br/>stance / author quality / shilling / polarization / events"]
        N2 --> N3["Net bullish index + event panel"]
    end

    subgraph L2["② Options layer · what the market is paying for"]
        O1["yfinance options chain snapshot"] --> O2["ATM IV / 25Δ RR / term slope / PC OI / naive GEX"]
        O2 --> O3["Sub-states: direction / volatility / positioning"]
    end

    P1 -- "yes" --> O1
    MS --> L1
    MO --> L2

    N3 --> G1{"Narrative confidence gate<br/>unique authors ≥10 and valid posts ≥20?"}
    O3 --> G2{"STALE gate<br/>options data ≤20h old and same US trading day?"}
    G1 -- "no" --> LOW["data_quality=LOW<br/>written into gates"]
    G1 -- "yes" --> FU
    G2 -- "no" --> ST["options_state=STALE<br/>alignment=NOT_FUSED<br/>fusion refused"]
    G2 -- "yes" --> FU

    subgraph L3["③ Fusion layer · state fusion, not score fusion"]
        FU["alignment: aligned/divergent × up/down<br/>crowding: HIGH / MEDIUM / LOW"] --> REG["Four-quadrant Regime<br/>ALIGNED / DIVERGENT / MIXED / NOT_FUSED"]
    end

    ST --> OUT
    LOW --> OUT
    REG --> OUT["Final output<br/>Regime + crowding + sell-side consensus (standalone)"]
    OUT --> WARN["⚠ Descriptive state, not a trading signal<br/>DIVERGENT is a research flag, not a contrarian signal"]
```

## Mode routing

| You ask | Mode | Command |
|---|---|---|
| "Show me NVDA sentiment" | sentiment | `uv run run.py --ticker NVDA --mode sentiment` |
| "What do NVDA options look like" | options | `uv run run.py --ticker NVDA --mode options` |
| "How is NVDA doing" (default) | full | `uv run run.py --ticker NVDA --mode full` |
| "Run the whole watchlist" | all | `uv run run.py --mode all` |
| "State of my portfolio" | portfolio | `uv run run.py --mode portfolio` |
| "Time series / panel" | panel | `uv run panel.py --ticker NVDA` |

## Example output

Real fused output from `--ticker NVDA --mode full` run off-hours on 2026-09-26 (fv2 / schema 1.2, excerpt):

```json
{
  "meta": {
    "schema_version": "1.2",
    "fusion_version": "fv2",
    "options_version": "ov2",
    "mode": "full",
    "run_at": "2026-09-26T10:45:50",
    "us_market_session": "closed"
  },
  "narrative": {
    "net_index": 0.035,
    "verdict": "中性",
    "polarization": 0.366,
    "polarization_level": "Medium",
    "extreme_consensus": false,
    "n_effective": 36,
    "unique_effective_authors": 40
  },
  "options": {
    "spot": 225.07,
    "atm_iv_near": 0.2664,
    "atm_iv_next": 0.2845,
    "term_slope_vpt": 1.8,
    "rr25_vpt": -2.1,
    "pc_oi": 1.068,
    "gex_usd_per_1pct": 203534
  },
  "options.data_quality": {
    "level": "MEDIUM",
    "status": "frozen",
    "stale": false,
    "data_as_of": "2026-09-25T16:00-04:00",
    "age_minutes": 406,
    "reasons": ["frozen_offhours"]
  },
  "options.substates": {
    "direction": { "state": "LEAN_BEARISH", "evidence": ["rr25 -2.1vpt"] },
    "volatility": { "atm_iv": 0.2664, "read": "normal", "term_slope_vpt": 1.8, "term": "flat" },
    "positioning": { "pc_oi": 1.068, "pc_vol": 0.513, "gex_sign": "positive", "read": "balanced" },
    "note": "direction from rr25+pc_oi only; IV/term/GEX informational"
  },
  "analyst": {
    "n_analysts": 59,
    "recommendation_key": "strong_buy",
    "breakdown": { "strongBuy": 10, "buy": 48, "hold": 2, "sell": 1, "strongSell": 0 },
    "target_mean": 327.7,
    "upside_pct": 0.456,
    "forward_pe": 14.351548
  },
  "data_quality": {
    "sentiment": { "level": "HIGH", "n_effective": 36, "unique_authors": 40, "age_hours": 0.0 },
    "narrative_options_same_session": true,
    "gates": []
  },
  "fusion": {
    "narrative_state": "NEUTRAL",
    "options_state": "LEAN_BEARISH",
    "options_evidence": ["rr25 -2.1vpt"],
    "alignment": "MIXED",
    "relationship": "MIXED",
    "pricing_direction": "LEAN_BEARISH",
    "crowding": "LOW",
    "fusion_gate": { "fused": true, "reasons": [] },
    "regime_label": "两侧均中性（无状态）",
    "fusion_version": "fv2"
  }
}
```

How to read it:

- Narrative is neutral (net index +0.035, within ±0.15; 40 unique authors / 36 effective posts → HIGH confidence), options lean bearish (rr25 −2.1 vpt, PC OI 1.07) → `alignment=MIXED`
- `regime_label` is display copy only; directional information stays in `pricing_direction=LEAN_BEARISH` — never squashed into a single score
- Off-hours options chains are frozen values (`status=frozen`, 6.8h since close ≤ 20h): honestly flagged `MEDIUM` and fusion proceeds; past 20h the output becomes `NOT_FUSED` and no Regime is issued
- `gates: []` = no gate triggered this run (narrative confidence and cross-session checks both passed)
- Sell-side consensus (59 analysts, strong_buy, +45.6% implied by mean target) is a standalone data point — it **did not enter** the fusion

## Install

### Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/); the only third-party dependency is `yfinance` (`pip install yfinance`, or `uv run --with yfinance ...`)
- **Narrative layer** (needed for sentiment / full): a `TYPESAFE_API_KEY` (Jev API); [opencli](https://github.com/anthropics/opencli) + Chrome extension, logged into x.com (`opencli doctor` to self-check; with multiple profiles, `opencli profile use <chrome>`)
- **Options layer / sell-side consensus**: just yfinance reachability (free, 15-min delayed, frozen values outside market hours)

### Steps

Option 1 — skills installer (**requires a public repo**; it only copies files into the skills directory — the runtime prerequisites above still apply):

```bash
npx skills add royal-byte/stock-intelligence
```

Option 2 — git clone (works for private repos too):

```bash
# Clone into your agent's skills directory
git clone https://github.com/royal-byte/stock-intelligence.git ~/.agents/skills/stock-intelligence
# For Claude Code specifically, clone into ~/.claude/skills/stock-intelligence

# Self-check
opencli doctor
```

Personalization (optional):

- `watchlist.json` — your watchlist and correlation clusters (portfolio mode uses `clusters` to flag "same cluster ≈ same trade")
- `sentiment/accounts.json` — KOL weighting (boost) / exclusion (zero), author names without @

## Usage

```bash
cd ~/.agents/skills/stock-intelligence
uv run run.py --ticker NVDA --mode full   # full research chain
uv run panel.py --ticker NVDA --csv out.csv
```

- Every run persists versioned snapshots under `data/` (`sentiment/`, `fusion/`, `portfolio/`; meta carries schema / prompt / fusion versions)
- Time series can only accumulate from your first run (X has no historical backfill); **running the watchlist daily with the same settings is the main way to build data**
- Best window is US market hours (21:30–04:00 Beijing time); outside market hours options are frozen values (≤20h degraded flag, >20h fusion refused)

## Interpretation discipline

- Net bullish index ∈ [-1,+1], midpoint 0, neutral within ±0.15; **not a probability of going up**; sampling confidence is reported separately
- Late fusion: narrative and options are presented independently; the Regime is four independent fields, never a single score
- **DIVERGENT is a research flag, not a contrarian trading signal** (work through the five explanations: sample bias / limited influence / institutions disagree / already priced in / narrative without money behind it)
- Event panels are unverified — always check primary sources (announcements / SEC / IR) before trading on them
- Extreme agreement (|net index| >0.35 or one-sided >80%) is a descriptive marker; the reversal relationship is untested
- Always-true boundaries: sentiment≠expected return · IV high≠bullish (IV is magnitude, not direction) · GEX is a hedging structure, not a direction forecast · sell-side ratings≠trading signals

Deep design docs: [references/sentiment-internals.md](references/sentiment-internals.md) (sentiment question definitions / weights / sampling), [references/backtest.md](references/backtest.md) (backtest routes and lag discipline)

## Disclaimer

This project outputs descriptive market-state research material. It is not investment advice. Markets carry risk; make your own decisions.

## License

[MIT](LICENSE)
