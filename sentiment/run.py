# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""X/finance post sentiment -> bullish/bearish judgment via Jev. (schema v2)

Usage:
  uv run run.py --ticker NVDA --source opencli --n 50 --product live --x-query "min_faves:5 -filter:replies" --author-stats
  uv run run.py --ticker NVDA --source paste --file test_events.txt
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import jev
import sources

ROOT = Path(__file__).parent            # .../stock-intelligence/sentiment
DATA = ROOT.parent / "data" / "sentiment"   # unified data dir at skill root
ACCOUNTS_FILE = ROOT / "accounts.json"

AGGREGATION_VERSION = "v2"   # bump when weight formula / thresholds change
STANCE_ZH = {
    "strongly_bullish": "强利多", "bullish": "利多", "neutral": "中性",
    "bearish": "利空", "strongly_bearish": "强利空", "not_about_stock": "无关",
}
CLAIM_ZH = {
    "fundamental": "基本面论据", "product": "产品/技术", "financial": "财务数据",
    "macro_narrative": "宏观叙事", "technical": "技术分析", "no_claim": "无主张",
}
EVIDENCE_ZH = {
    "none": "无证据", "reasoning": "纯推理", "hearsay": "传闻/叙事", "named_source": "具名可验证",
}


# ---------------------------------------------------------------- L1 dedup --
def _norm(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text.lower())
    text = re.sub(r"[\d\uFF10-\uFF19]+", "#", text)      # digits -> # (template variants)
    return re.sub(r"\s+", " ", text).strip()


def dedup(posts: list[dict]) -> tuple[list[dict], dict]:
    """Exact + template dedup before Jev (saves tokens, blocks coord flooding).

    Template key = normalized text w/o digits/urls, first 80 chars — catches the
    'I'm only going to say this TWICE ... under 41/42/45' family across accounts.
    """
    seen_exact: set[str] = set()
    tmpl_count: Counter[str] = Counter()
    out: list[dict] = []
    n_exact = 0
    for p in posts:
        norm = _norm(p.get("text", ""))
        if not norm:
            continue
        h = hashlib.md5(norm.encode()).hexdigest()
        if h in seen_exact:
            n_exact += 1
            continue
        seen_exact.add(h)
        tkey = hashlib.md5(norm[:80].encode()).hexdigest()
        if tmpl_count[tkey] >= 1:      # first instance kept, variants counted
            tmpl_count[tkey] += 1
            continue
        tmpl_count[tkey] += 1
        out.append(p)
    clusters = {k: c for k, c in tmpl_count.items() if c > 1}
    stats = {
        "n_raw": len(posts),
        "n_after_dedup": len(out),
        "n_exact_dupes": n_exact,
        "n_template_dupes": sum(c - 1 for c in clusters.values()),
        "coordination_clusters": len(clusters),   # same-template multi-post groups
        "max_cluster_size": max(clusters.values(), default=1),
    }
    return out, stats


# ------------------------------------------------------------------- L2 jev --
def analyze(posts: list[dict], ticker: str, company: str) -> list[dict]:
    """Batch fan-out (N posts x questions per request); per-post fallback."""
    try:
        answers_list = jev.judge_posts_batch(posts, ticker, company)
    except Exception as e:
        print(f"[warn] batch request failed ({e}); falling back to per-post")
        answers_list = [None] * len(posts)
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(jev.judge_post, p, ticker, company) for p in posts]
            for i, fut in enumerate(futures):
                try:
                    answers_list[i] = fut.result()
                except Exception:
                    pass
    judged = []
    for post, answers in zip(posts, answers_list):
        if answers:
            judged.append(dict(post, judgments=answers))
        else:
            judged.append(dict(post, judgments=None, error="request failed"))
    return judged


# ---------------------------------------------------------------- L3 weight --
def stance_expectation(ans: dict) -> tuple[float, str]:
    probs = ans["stance"]["probabilities"]
    exp = sum(probs.get(opt, 0.0) * jev.STANCE_SCORES[opt] for opt in jev.STANCE_SCORES)
    return exp, ans["stance"]["choice"]


def post_weight(ans: dict, post: dict, lists: dict) -> float:
    genuine = ans["genuine_view"]["noul"]
    catalyst = ans["has_catalyst"]["noul"]
    novelty = min(max(ans["info_novelty"]["score"] / 2.0, 0.0), 1.0)
    promo = ans["promo_suspect"]["noul"]
    related = 1.0 - ans["stance"]["probabilities"].get("not_about_stock", 0.0)
    w = related * genuine * (0.7 + 0.3 * catalyst) * (0.5 + 0.5 * novelty) * (1 - 0.8 * promo)

    likes = (post.get("engagement") or {}).get("likes")
    if isinstance(likes, (int, float)):
        w *= 0.7 + 0.3 * min(1.0, math.log10(1 + likes) / 4.0)

    expertise = ans.get("author_expertise")
    if expertise:
        w *= 0.7 + 0.3 * (min(max(expertise["score"], 0.0), 2.0) / 2.0)

    followers = (post.get("author_stats") or {}).get("followers")
    if isinstance(followers, (int, float)):
        w *= 0.7 + 0.3 * min(1.0, math.log10(1 + followers) / 5.7)

    author = post.get("author", "")
    if author in lists["zero"]:
        return 0.0
    if author in lists["boost"]:
        w *= lists["boost"][author]
    return w


def aggregate(judged: list[dict], lists: dict) -> dict:
    num = den = 0.0
    bull_w = bear_w = neut_w = 0.0
    n_effective = 0
    stance_exp_w: list[tuple[float, float]] = []   # (weight, stance expectation)
    for p in judged:
        if not p.get("judgments"):
            continue
        exp, choice = stance_expectation(p["judgments"])
        w = post_weight(p["judgments"], p, lists)
        p["_stance_exp"] = round(exp, 3)
        p["_choice"] = choice
        p["_weight"] = round(w, 4)
        if choice != "not_about_stock" and w > 0.01:
            n_effective += 1
            stance_exp_w.append((w, exp))
        num += w * exp
        den += w
        if exp > 0.15:
            bull_w += w
        elif exp < -0.15:
            bear_w += w
        else:
            neut_w += w
    net = num / den if den > 1e-9 else 0.0

    # polarization: weighted std of stance expectations over effective posts
    pol = 0.0
    if stance_exp_w and (tw := sum(w for w, _ in stance_exp_w)) > 0:
        mean = sum(w * e for w, e in stance_exp_w) / tw
        var = sum(w * (e - mean) ** 2 for w, e in stance_exp_w) / tw
        pol = var ** 0.5
    side_w = bull_w + bear_w + neut_w
    unique_authors = len({p.get("author") for p in judged
                          if p.get("author") and p.get("_weight", 0) > 0.01})

    n_total = sum(1 for p in judged if p.get("judgments"))
    if den < 1.0 or n_effective < 5:
        verdict = "样本不足（无有效舆情信号）"
    else:
        verdict = "利多" if net > 0.15 else ("利空" if net < -0.15 else "中性")

    if n_effective >= 15 and unique_authors >= 10:
        conf = "High"
    elif n_effective >= 8 and unique_authors >= 6:
        conf = "Medium"
    else:
        conf = "Low"

    extreme = (bull_w / side_w > 0.85 and net > 0.35) if side_w else False

    return {
        "net_index": round(net, 3),
        "verdict": verdict,
        "sampling_confidence": conf,
        "bullish_weight": round(bull_w, 2),
        "bearish_weight": round(bear_w, 2),
        "bull_bear_ratio": round(bull_w / bear_w, 1) if bear_w > 0.005 else None,
        "neutral_weight": round(neut_w, 2),
        "bull_share": round(bull_w / side_w, 2) if side_w else 0.0,
        "neutral_share": round(neut_w / side_w, 2) if side_w else 0.0,
        "bear_share": round(bear_w / side_w, 2) if side_w else 0.0,
        "polarization": round(pol, 3),
        "polarization_level": "High" if pol > 0.45 else ("Medium" if pol > 0.3 else "Low"),
        "extreme_consensus": extreme,
        "total_weight": round(den, 2),
        "n_posts": len(judged),
        "n_effective": n_effective,
        "n_failed": sum(1 for p in judged if not p.get("judgments")),
        "unique_effective_authors": unique_authors,
        "effective_share": round(n_effective / n_total, 2) if n_total else 0.0,
    }


# -------------------------------------------------------- L4 reasons/claims --
def reasons_summary(judged: list[dict]) -> list[str]:
    """What people are bullish/bearish ABOUT (claim/evidence), separate from stance."""
    bull: Counter = Counter()
    bear: Counter = Counter()
    ev_of: dict[tuple[str, str], Counter] = {}
    for p in judged:
        a = p.get("judgments")
        if not a or p.get("_weight", 0) <= 0.05:
            continue
        ct = a.get("claim_type", {}).get("choice", "no_claim")
        et = a.get("evidence_type", {}).get("choice", "none")
        ev_of[(ct, et)] = ev_of.get((ct, et), Counter())
        if p["_stance_exp"] > 0.15:
            bull[ct] += 1
            ev_of[(ct, et)]["bull"] += 1
        elif p["_stance_exp"] < -0.15:
            bear[ct] += 1
            ev_of[(ct, et)]["bear"] += 1
    lines = []
    for label, counter in (("多头论据", bull), ("空头论据", bear)):
        if not counter:
            lines.append(f"{label}: （无）")
            continue
        parts = []
        for ct, n in counter.most_common(4):
            named = sum(c.get("bull" if label == "多头论据" else "bear", 0)
                        for (c2, e), c in ev_of.items() if c2 == ct and e == "named_source")
            parts.append(f"{CLAIM_ZH.get(ct, ct)}×{n}" + (f"（{named}条具名证据）" if named else ""))
        lines.append(f"{label}: " + "、".join(parts))
    named_total = sum(c["bull"] + c["bear"] for (ct, e), c in ev_of.items() if e == "named_source")
    reasoning_total = sum(c["bull"] + c["bear"] for (ct, e), c in ev_of.items() if e in ("none", "hearsay"))
    if named_total + reasoning_total:
        lines.append(f"证据构成: 具名可验证 {named_total} 条 vs 无证据/传闻 {reasoning_total} 条")
    return lines


# ----------------------------------------------------------------- L4 events --
EVENT_TYPE_ZH = {
    "demand": "需求信号", "supply": "供应链", "company": "公司事件",
    "smart_money": "资本动向", "regulation": "监管政策", "competition": "竞争格局",
    "macro": "宏观", "none": "无催化",
}
DIRECTION_ZH = {"clearly_bullish": "利多", "clearly_bearish": "利空", "mixed_or_unclear": "双向/不明"}
HORIZON_ZH = {"days": "数日", "quarter": "季度级", "structural": "长期结构"}


def event_panel(judged: list[dict], ticker: str) -> list[str]:
    """Second-stage event extraction -> event objects (typed, counted, unverified)."""
    catalyzed = [p for p in judged if p.get("judgments")
                 and p["judgments"]["has_catalyst"]["noul"] >= 0.5]
    if not catalyzed:
        return ["", "--- 事件面板（客观催化剂，未经验证） ---", "（无含具体催化剂的帖子）"]
    try:
        events = jev.judge_events_batch(catalyzed, ticker)
    except Exception as e:
        return ["", "--- 事件面板 ---", f"（事件抽取失败: {e}）"]

    rows = []
    for p, ev in zip(catalyzed, events):
        if not ev:
            continue
        p["event"] = ev
        if ev["event_type"]["choice"] == "none":
            continue
        imp = ev["event_impact"]["score"] / 2.0
        directional = 1.0 if ev["event_direction"]["choice"] != "mixed_or_unclear" else 0.6
        score = imp * directional * ev["event_impact"]["confidence"]
        rows.append((score, p, ev))
    rows.sort(key=lambda r: r[0], reverse=True)

    # bucket counts per (type, direction): how many posts/authors carry each theme
    buckets: Counter = Counter()
    bucket_authors: dict[tuple, set] = {}
    for _, p, ev in rows:
        key = (ev["event_type"]["choice"], ev["event_direction"]["choice"])
        buckets[key] += 1
        bucket_authors.setdefault(key, set()).add(p.get("author") or p.get("id"))

    lines = ["", "--- 事件面板（客观催化剂，未经验证） ---"]
    if not rows:
        lines.append("（未抽取到方向明确的催化剂）")
        return lines
    for score, p, ev in rows[:8]:
        key = (ev["event_type"]["choice"], ev["event_direction"]["choice"])
        n, na = buckets[key], len(bucket_authors[key])
        flags = ""
        if ev["smart_money_named"]["noul"] >= 0.5:
            flags += "·具名资本动向"
        if ev["event_impact"]["score"] >= 1.0:
            flags += "·高影响·未验证⚠"
        mention = f"提及{n}帖/{na}作者" if n > 1 else "单帖"
        lines.append(
            f"[{DIRECTION_ZH[ev['event_direction']['choice']]}"
            f" · {EVENT_TYPE_ZH.get(key[0], key[0])}"
            f" · 影响{ev['event_impact']['score']:.1f}/2"
            f" · {HORIZON_ZH[ev['event_horizon']['choice']]}"
            f" · {mention}{flags}] "
            f"{p['text'][:90].replace(chr(10), ' ')}… ({p['author'] or '无作者'})"
        )
    hi_unverified = [r for r in rows if r[2]["event_impact"]["score"] >= 1.0]
    if hi_unverified:
        lines.append(
            f"⚠ {len(hi_unverified)} 条高影响事件均未验证——报告的是'X 上在传什么'，"
            f"交易决策前必须回源核实（公告/SEC/公司 IR）。"
        )
    return lines


# ----------------------------------------------------------------- L5 report --
def report(ticker: str, agg: dict, judged: list[dict], dedup_stats: dict, sampling: str) -> str:
    lines = []
    lines.append(f"=== {ticker} X 舆情（{jev.SCHEMA_VERSION}/{jev.PROMPT_VERSION}/{AGGREGATION_VERSION}，"
                 f"{dt.datetime.now():%Y-%m-%d %H:%M}） ===")
    side = agg.get("bull_share", 0) + agg.get("neutral_share", 0) + agg.get("bear_share", 0)
    neut_pct = agg.get("neutral_share", 0) / side if side else 0
    lines.append(
        f"【情绪】净利多指数 {agg['net_index']:+.3f} → {agg['verdict']}"
        f" ｜ 采样置信度 {agg['sampling_confidence']}"
        f" ｜ 极化 {agg['polarization_level']}({agg['polarization']:.2f})"
        f"（多{agg.get('bull_share', 0):.0%}/中性{neut_pct:.0%}/空{agg.get('bear_share', 0):.0%}）"
    )
    ratio = agg.get("bull_bear_ratio")
    if ratio is not None:
        ratio_txt = f"多空权重比 {ratio:.1f}:1（{agg['bullish_weight']:.2f} vs {agg['bearish_weight']:.2f}）"
    elif agg.get("bullish_weight", 0) > 0.005:
        ratio_txt = f"多空权重比 全多（{agg['bullish_weight']:.2f} vs 0.00）"
    else:
        ratio_txt = "多空权重比 n/a"
    lines.append(
        f"  {ratio_txt} ｜ 样本: 有效帖 {agg['n_effective']}/{agg['n_posts']}"
        f"（抓取 {dedup_stats['n_raw']}，去重移除 {dedup_stats['n_exact_dupes'] + dedup_stats['n_template_dupes']}，"
        f"协同模板群 {dedup_stats['coordination_clusters']}）"
        f"｜ 独立作者 {agg['unique_effective_authors']}"
    )
    if agg["extreme_consensus"]:
        lines.append(
            "  ⚠ 极端一致状态（多空权重比 >6:1 且指数 >0.35）——这是描述性标记；"
            "极端情绪与反转的关系未经回测，勿直接当反向交易规则。"
        )
    if agg.get("effective_share", 1.0) < 0.3:
        lines.append(
            f"  ⚠ 有效样本占比仅 {agg.get('effective_share', 0):.0%}——多半是 ticker 词歧义或采样过窄，"
            f"结论不可用。改用 --company 提供公司名，或 --x-query 加 lang: 过滤。"
        )
    lines.append("")
    lines.append("【多空论据】")
    lines.extend("  " + l for l in reasons_summary(judged))
    lines.append(f"\n【采样口径】{sampling} ｜ X 排序子集（平台过滤+账号个性化，绝对值有偏）")
    lines.append("【边界】非上涨概率·非价格预测·事件提及≠事件为真·作者专业≠主张为真·互动量≠信息质量")
    ok = [p for p in judged if p.get("judgments")]
    ok.sort(key=lambda p: abs(p.get("_stance_exp", 0)) * p.get("_weight", 0), reverse=True)
    lines.append("\n--- 头部权重观点 ---")
    for p in ok[:5]:
        a = p["judgments"]
        flags = []
        if a["genuine_view"]["noul"] < 0.5: flags.append("噪声")
        if a["has_catalyst"]["noul"] >= 0.5: flags.append("有催化剂")
        if a["promo_suspect"]["noul"] >= 0.4: flags.append("喊单嫌疑")
        if a.get("author_expertise", {}).get("score", 0) >= 1.2: flags.append("专业作者")
        ct = a.get("claim_type", {}).get("choice")
        if ct and ct != "no_claim": flags.append(CLAIM_ZH.get(ct, ct))
        et = a.get("evidence_type", {}).get("choice")
        if et == "named_source": flags.append("具名证据")
        likes = (p.get("engagement") or {}).get("likes")
        lines.append(
            f"[{STANCE_ZH.get(p['_choice'], p['_choice'])} {p['_stance_exp']:+.2f} w={p['_weight']:.2f}"
            f" conf={a['stance']['confidence']:.2f}"
            f"{' · ' + '·'.join(flags) if flags else ''}] "
            f"{p['text'][:100].replace(chr(10), ' ')}…  ({p['author'] or p['url'][:40]})"
        )
    return "\n".join(lines)


def load_lists() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            cfg = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            return {"boost": cfg.get("boost", {}), "zero": set(cfg.get("zero", []))}
        except Exception as e:
            print(f"[warn] accounts.json unreadable ({e}); ignoring")
    return {"boost": {}, "zero": set()}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--company", default="")
    ap.add_argument("--source", choices=["exa", "opencli", "paste"], default="exa")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--product", choices=["top", "live"], default="top")
    ap.add_argument("--x-query", default="")
    ap.add_argument("--file", help="paste source file")
    ap.add_argument("--author-stats", action="store_true")
    args = ap.parse_args()

    if args.source == "exa":
        posts = sources.fetch_exa(args.ticker, args.n)
    elif args.source == "opencli":
        posts = sources.fetch_opencli(args.ticker, args.n, product=args.product,
                                      extra=args.x_query, company=args.company)
    else:
        if not args.file:
            ap.error("--source paste requires --file")
        posts = sources.load_paste(args.file)

    posts, dedup_stats = dedup(posts)
    print(f"[source={args.source}] {dedup_stats['n_raw']} posts -> {len(posts)} after dedup "
          f"({dedup_stats['n_exact_dupes']} exact, {dedup_stats['n_template_dupes']} template dupes removed)")

    if args.author_stats and args.source == "opencli":
        authors = sorted({p["author"] for p in posts if p.get("author")})
        print(f"[author-stats] fetching {min(len(authors), 12)} profiles (rate-limited)...")
        stats = sources.fetch_author_stats(authors)
        for p in posts:
            st = stats.get(p.get("author", ""))
            if st:
                p["author_stats"] = st
                if st.get("bio") and not p.get("bio"):
                    p["bio"] = st["bio"]

    judged = analyze(posts, args.ticker, args.company)
    lists = load_lists()
    agg = aggregate(judged, lists)

    ev_lines = event_panel(judged, args.ticker)

    sampling = (f"{args.source}/{args.product}"
                + (f"/{args.x_query}" if args.x_query else "")
                + ("/author-stats" if args.author_stats else ""))
    DATA.mkdir(exist_ok=True)
    out = DATA / f"{args.ticker.lower()}_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    meta = {
        "schema_version": jev.SCHEMA_VERSION,
        "prompt_version": jev.PROMPT_VERSION,
        "aggregation_version": AGGREGATION_VERSION,
        "sampling_profile": sampling,
        "run_args": vars(args),
        "dedup": dedup_stats,
        "run_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    out.write_text(json.dumps({"meta": meta, "aggregate": agg, "posts": judged},
                              ensure_ascii=False, indent=1), encoding="utf-8")

    print(report(args.ticker, agg, judged, dedup_stats, sampling))
    for line in ev_lines:
        print(line)
    print(f"\n原始判断已存: {out}")


if __name__ == "__main__":
    main()
