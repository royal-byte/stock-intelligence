"""Jev (TypeSafe System One) client: per-post typed judgments via one fanned-out request.

Docs: https://docs.typesafe.ai/api.md
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# --- Method versioning -----------------------------------------------------
# Changing anything in _questions()/event_questions() requires bumping
# PROMPT_VERSION; changing aggregation requires AGGREGATION_VERSION. Archived
# judgments are only comparable across runs when these match.
PROMPT_VERSION = "v2"
SCHEMA_VERSION = "2.0"
# ----------------------------------------------------------------------------

STANCE_SCORES = {
    "strongly_bullish": 1.0,
    "bullish": 0.5,
    "neutral": 0.0,
    "bearish": -0.5,
    "strongly_bearish": -1.0,
    "not_about_stock": 0.0,
}


def _questions() -> dict:
    return {
        "stance": {
            "type": "choice",
            "instructions": (
                "What directional view does `post.text` express about the future price of "
                "`ticker` stock? Judge the author's own stance toward the stock's outlook, "
                "including sarcasm reversed in meaning. Factual news without a view is neutral. "
                "If the text is not about `ticker` (the company or its stock), pick not_about_stock."
            ),
            "criteria": {
                "strongly_bullish": "Explicitly bullish, discloses/urges a long position, expects large upside with conviction",
                "bullish": "Leans bullish: expects the stock to rise or outperform",
                "neutral": "Neutral, factual, or balanced long-and-short views with no clear direction",
                "bearish": "Leans bearish: expects the stock to fall or underperform",
                "strongly_bearish": "Explicitly bearish, discloses/urges shorting or selling, warns of large downside",
                "not_about_stock": "Not about `ticker` the company or its stock, or no judgment possible",
            },
        },
        "genuine_view": {
            "type": "noul",
            "instructions": (
                "Is `post.text` a genuine, independently expressed market view or analysis "
                "(opinion, reasoning, prediction, first-hand account), rather than a meme/joke, "
                "pure emotional venting, advertisement, or a bare restatement of widely known news?"
            ),
            "criteria": {
                "true": "Contains a genuine view, reasoning, or first-hand market information",
                "false": "Meme, joke, pure hype/emotion, ad, or bare restatement of known news",
            },
        },
        "has_catalyst": {
            "type": "noul",
            "instructions": (
                "Does `post.text` mention a concrete catalyst that could move `ticker` stock "
                "(earnings, product launch, orders, guidance, capex plans, regulation, "
                "executive statements, supply/demand, competition, macro, OR a disclosed "
                "position change by a fund/known investor/insider such as 13F filings or "
                "politician/Cathie Wood-style portfolio updates)?"
            ),
            "criteria": {
                "true": "Names at least one concrete catalyst",
                "false": "No concrete catalyst, only mood or price commentary",
            },
        },
        "info_novelty": {
            "type": "score",
            "instructions": "How much new information does `post.text` carry about `ticker`?",
            "criteria": [
                "Pure restatement of old news or no substantive information",
                "Mostly known information with a small amount of new detail or framing",
                "Contains clear new information, data, or original analysis",
            ],
        },
        "promo_suspect": {
            "type": "noul",
            "instructions": (
                "Does `post.text` show signs of stock promotion or manipulation: urging readers "
                "to buy with exaggerated claims and no reasoning, hype language, giveaway/DM "
                "bait, or coordinated pumping of `ticker`?"
            ),
            "criteria": {
                "true": "Promotion/pump pattern present",
                "false": "No promotion pattern",
            },
        },
        "author_expertise": {
            "type": "score",
            "instructions": (
                "How much financial expertise does the author of `post.text` appear to have? "
                "Use `post.bio` if present, plus writing style. Professional signals: precise "
                "position sizing and risk language, data citations, domain terminology, "
                "analyst/media tone. Non-signals: all-caps hype, emoji spam, generic calls."
            ),
            "criteria": [
                "No professional signal: hype, memes, generic buy/sell calls",
                "Retail-level: familiar with markets but little depth or rigor",
                "Sophisticated: structured reasoning, data use, terminology typical of an experienced investor, professional commentator, or finance media",
            ],
        },
        "claim_type": {
            "type": "choice",
            "instructions": (
                "What is the main type of claim or reasoning about `ticker` in `post.text`? "
                "This is separate from the author's stance: identify WHAT the argument "
                "is about, not whether it is bullish or bearish."
            ),
            "criteria": {
                "fundamental": "Fundamental thesis: demand, market share, margins, moat, industry structure",
                "product": "Product/technology: launches, roadmaps, technical capabilities",
                "financial": "Financial data: earnings, guidance, filings, price targets, orders",
                "macro_narrative": "Macro/sector narrative: AI cycle, rates, geopolitics, sector rotation",
                "technical": "Chart/technical analysis: patterns, levels, momentum, waves",
                "no_claim": "No substantive claim about `ticker`",
            },
        },
        "evidence_type": {
            "type": "choice",
            "instructions": "What backs the main claim in `post.text` about `ticker`?",
            "criteria": {
                "none": "No evidence: pure opinion, hype, or bare assertion",
                "reasoning": "Reasoning only: logic or argument without external data",
                "hearsay": "Hearsay/narrative: 'everyone knows', unnamed sources, market chatter",
                "named_source": "Named verifiable source: a specific filing, report, dataset, event, or first-hand observation",
            },
        },
        "evidence_strength": {
            "type": "score",
            "instructions": (
                "How strong and checkable is the evidence behind the main claim in `post.text`? "
                "Judge the evidence, not the stance. A bullish claim with a filing citation "
                "outranks a bearish claim backed only by vibes, and vice versa."
            ),
            "criteria": [
                "Unverifiable: no evidence or pure assertion",
                "Partially checkable: reasoning or second-hand claims",
                "Directly checkable: specific named data, filing, event, or first-hand observation",
            ],
        },
    }


def judge_post(post: dict, ticker: str, company: str = "") -> dict:
    """One Jev request per post; 5 parallel questions. Returns raw answer dict."""
    state = {
        "ticker": ticker,
        "company": company,
        "post": post,
    }
    payload = {"state": state, "model": MODEL, "questions": _questions()}
    data = _post(payload)
    return data["answers"]


def judge_posts_batch(posts: list[dict], ticker: str, company: str = "", chunk: int = 8) -> list[dict]:
    """Batch many posts into few requests (official fan-out pattern).

    One request carries `chunk` posts in state + chunk×5 questions. Adding questions
    barely changes latency (docs: parallel evaluation), and state is sent once instead
    of per-post — 15 posts = 2 requests instead of 15.
    """
    results: list[dict | None] = [None] * len(posts)
    template = _questions()
    qids = list(template)
    for start in range(0, len(posts), chunk):
        batch = posts[start : start + chunk]
        state = {"ticker": ticker, "company": company, "posts": batch}
        questions = {}
        for i in range(len(batch)):
            for qid, q in template.items():
                questions[f"p{i}_{qid}"] = _rebind_post_paths(q, i)
        data = _post({"state": state, "model": MODEL, "questions": questions})
        for i in range(len(batch)):
            results[start + i] = {qid: data["answers"][f"p{i}_{qid}"] for qid in qids}
    return results


def event_questions() -> dict:
    """Second-stage questions, asked only for posts where has_catalyst >= 0.5
    (dependent-question pattern: a second request is warranted because the
    first answer decides which posts get the event treatment)."""
    return {
        "event_type": {
            "type": "choice",
            "instructions": (
                "Classify the concrete catalyst mentioned in `post.text` regarding `ticker`. "
                "Trace the value chain: if the event is upstream or downstream of `ticker` "
                "(e.g. a material shortage, a customer's capex, a supplier's capacity), "
                "classify by the chain link it hits."
            ),
            "criteria": {
                "demand": "Customer demand: hyperscaler/enterprise capex, orders, usage growth, adoption",
                "supply": "Supply chain: shortage, bottleneck, capacity, cost — or its easing",
                "company": "Company event: product/tech launch, earnings, guidance, management, M&A",
                "smart_money": "Position disclosure by a named fund/insider/known investor (filings, portfolio updates)",
                "regulation": "Regulation, policy, export controls, antitrust",
                "competition": "Competitive landscape: rival wins/losses, market-share shifts",
                "macro": "Macro: rates, inflation, FX, geopolitics moving the whole sector",
                "none": "No concrete catalyst identifiable",
            },
        },
        "event_direction": {
            "type": "choice",
            "instructions": (
                "Net effect of this event on `ticker`'s demand, pricing power, competitive "
                "position, or investment narrative. Trace the value chain when the event is "
                "upstream or downstream. When the dominant effect is clearly positive or "
                "negative, do NOT pick mixed merely because minor secondary effects exist. "
                "Reserve mixed for genuinely two-sided or unverifiable situations."
            ),
            "criteria": {
                "clearly_bullish": "Dominant net effect on `ticker` is positive",
                "clearly_bearish": "Dominant net effect on `ticker` is negative",
                "mixed_or_unclear": "Genuinely two-sided, unverifiable, or too vague to call",
            },
        },
        "event_impact": {
            "type": "score",
            "instructions": "How large is this event's effect on `ticker`'s investment case?",
            "criteria": [
                "Minor or likely already priced in",
                "Meaningful: could move estimates, orders, or the narrative",
                "Major: changes guidance, market structure, or the core investment case",
            ],
        },
        "event_horizon": {
            "type": "choice",
            "instructions": "Over what horizon does this event play out for `ticker`?",
            "criteria": {
                "days": "Days: immediate reaction, fades fast",
                "quarter": "Quarter-scale: shows up in next 1-2 earnings cycles",
                "structural": "Multi-year structural shift",
            },
        },
        "smart_money_named": {
            "type": "noul",
            "instructions": (
                "Does `post.text` name a specific institutional investor, fund, or insider "
                "AND their actual position change relevant to `ticker` (e.g. 'Cathie Wood's "
                "ARK bought X', 'Pelosi disclosed N shares of `ticker`')? Mere price talk by "
                "a famous person does not count."
            ),
        },
    }


def judge_events_batch(posts: list[dict], ticker: str, company: str = "", chunk: int = 8) -> list[dict]:
    """Stage-2 fan-out over event questions for catalyst posts only."""
    results: list[dict | None] = [None] * len(posts)
    template = event_questions()
    qids = list(template)
    for start in range(0, len(posts), chunk):
        batch = posts[start : start + chunk]
        state = {"ticker": ticker, "company": company, "posts": batch}
        questions = {}
        for i in range(len(batch)):
            for qid, q in template.items():
                questions[f"p{i}_{qid}"] = _rebind_post_paths(q, i)
        data = _post({"state": state, "model": MODEL, "questions": questions})
        for i in range(len(batch)):
            results[start + i] = {qid: data["answers"][f"p{i}_{qid}"] for qid in qids}
    return results


def _rebind_post_paths(question: dict, i: int) -> dict:
    """Rewrite `post.*` references in instructions to `posts[i].*` for batch state."""
    q = json.loads(json.dumps(question))  # deep copy
    for field in ("instructions", "criteria"):
        val = q.get(field)
        if isinstance(val, str):
            q[field] = val.replace("`post`", f"`posts[{i}]`").replace("`post.", f"`posts[{i}].")
    return q


def _post(payload: dict, retries: int = 4) -> dict:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY not set")
    body = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in (429, 529):  # rate limit / overloaded -> backoff
                time.sleep(2**attempt + 0.5)
                continue
            raise RuntimeError(last_err)
        except urllib.error.URLError as e:
            last_err = f"network error: {e}"
            time.sleep(2**attempt)
    raise RuntimeError(f"Jev request failed after {retries} attempts: {last_err}")
