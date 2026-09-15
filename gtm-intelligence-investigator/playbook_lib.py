"""
Shared helpers for the staged Investigator playbooks (questions 2-9;
question 1's retention_drop_playbook.py predates this file and still
keeps its own copies of the functions it originated -- see the note atop
that file's stage 1-5 definitions).

Functions here are the pieces multiple question-playbooks reuse
verbatim: revenue/usage totals by segment, the corrected (revenue-based,
never units-based -- see account_had_usage_drop's docstring below)
>=20% MoM usage-drop check, the per-channel rate-stability check, NRR/GRR
and ARR-waterfall math that matches analytics_server.py's tools exactly,
and the common-attribute gatherer + bounded Claude call used by every
question's final synthesis stage. Anything specific to a single
question's own stages lives in that question's own file, not here.
"""

import os
import sqlite3

MODEL = "claude-sonnet-5"

# __file__-relative so this works regardless of the current working
# directory -- same pattern retention_drop_playbook.py uses.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_REPO_ROOT, "data", "metric_sleuth.db")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------
# Revenue / usage helpers (monthly_usage + accounts), from question 1
# ---------------------------------------------------------------------

def segment_totals(conn, segment: str, month: str):
    """(units, revenue) totals for a segment in one month."""
    row = conn.execute("""
        SELECT SUM(u.units) AS units, SUM(u.revenue) AS revenue
        FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
        WHERE a.segment = ? AND u.month = ?
    """, (segment, month)).fetchone()
    return row["units"], row["revenue"]


def segment_revenue(conn, segment: str, month: str) -> float | None:
    row = conn.execute("""
        SELECT SUM(u.revenue) AS rev FROM monthly_usage u
        JOIN accounts a ON a.account_id = u.account_id
        WHERE a.segment = ? AND u.month = ?
    """, (segment, month)).fetchone()
    return row["rev"]


def segment_pct_change(conn, segment: str, base_month: str, current_month: str) -> float | None:
    base, cur = segment_revenue(conn, segment, base_month), segment_revenue(conn, segment, current_month)
    if base is None or cur is None or base == 0:
        return None
    return (cur - base) / base * 100


def segment_channel_totals(conn, segment: str, channel: str, month: str):
    """(units, revenue) totals for one segment, one channel, one month --
    the only apples-to-apples comparison, since units mean different
    things per channel (sms messages, voice minutes, email sends,
    verification checks). Never sum units across channels."""
    row = conn.execute("""
        SELECT SUM(u.units) AS units, SUM(u.revenue) AS revenue
        FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
        WHERE a.segment = ? AND u.channel = ? AND u.month = ?
    """, (segment, channel, month)).fetchone()
    return row["units"], row["revenue"]


RATE_EFFECT_GAP_PTS = 10.0


def channel_level_trend(conn, segment: str, base_month: str, current_month: str) -> dict:
    """Per-channel units-vs-revenue comparison for a segment. Because
    channel_rates is a static table (list_price_usd never varies by time
    anywhere in this dataset), revenue and units for a single channel
    should move in exact lockstep -- any real divergence would signal a
    genuine rate effect (there isn't one to find in this dataset, by
    construction, so expect rate_effect_detected=False always; this stage
    exists to CONFIRM that rather than assume it, and to identify which
    channel is actually driving the decline)."""
    channels = [r["channel"] for r in conn.execute("SELECT DISTINCT channel FROM monthly_usage")]
    per_channel = {}
    for ch in channels:
        base_u, base_r = segment_channel_totals(conn, segment, ch, base_month)
        cur_u, cur_r = segment_channel_totals(conn, segment, ch, current_month)
        if not base_u or not base_r:
            continue
        units_change = (cur_u - base_u) / base_u * 100
        rev_change = (cur_r - base_r) / base_r * 100
        per_channel[ch] = {
            "units_change_pct": round(units_change, 1),
            "revenue_change_pct": round(rev_change, 1),
            "gap_pts": round(rev_change - units_change, 1),
        }

    rate_effect_detected = any(abs(c["gap_pts"]) >= RATE_EFFECT_GAP_PTS for c in per_channel.values())
    declining = {ch: c for ch, c in per_channel.items() if c["revenue_change_pct"] < 0}
    dominant_declining_channel = min(declining, key=lambda ch: declining[ch]["revenue_change_pct"]) if declining else None

    return {
        "per_channel": per_channel,
        "rate_effect_detected": rate_effect_detected,
        "dominant_declining_channel": dominant_declining_channel,
        "note": ("channel_rates has no time dimension in this dataset -- rate_effect_detected "
                 "confirms whether price is a factor rather than assuming it isn't; True here "
                 "would flag a data anomaly worth a separate look, not a usable pricing story."),
    }


def segment_mom_growth(conn, segment: str, month: str) -> float | None:
    prev_month = conn.execute(
        "SELECT strftime('%Y-%m', date(? || '-01', '-1 month')) AS m", (month,)
    ).fetchone()["m"]
    cur, prev = segment_revenue(conn, segment, month), segment_revenue(conn, segment, prev_month)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100


# ---------------------------------------------------------------------
# Concentration & pace (question 1's stage 2 rule, generalized to any
# segment so other questions -- e.g. question 4's NRR decomposition --
# can reuse it instead of re-deriving the same logic)
# ---------------------------------------------------------------------

SINGLE_ACCOUNT_STOP_THRESHOLD = 0.40
TOP5_CONCENTRATED_THRESHOLD = 0.60


def concentration_and_pace(conn, segment: str, base_month: str, current_month: str) -> dict:
    base = conn.execute("""
        SELECT u.account_id, SUM(u.revenue) AS base_rev
        FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
        WHERE a.segment = ? AND u.month = ? GROUP BY u.account_id
    """, (segment, base_month)).fetchall()
    cur = {r["account_id"]: r["cur_rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS cur_rev FROM monthly_usage WHERE month = ? GROUP BY account_id",
        (current_month,)
    )}

    deltas = []
    for r in base:
        cur_rev = cur.get(r["account_id"], 0.0)
        delta = cur_rev - r["base_rev"]
        if delta < 0:
            deltas.append({"account_id": r["account_id"], "delta": delta})
    deltas.sort(key=lambda d: d["delta"])

    total_loss = sum(-d["delta"] for d in deltas)
    if total_loss == 0 or not deltas:
        return {"no_losses": True, "stop_here": False}

    top1_share = -deltas[0]["delta"] / total_loss
    top5_share = sum(-d["delta"] for d in deltas[:5]) / total_loss
    single_dominant = top1_share > SINGLE_ACCOUNT_STOP_THRESHOLD
    shape = ("single_dominant_account" if single_dominant
             else "concentrated_few_big_movers" if top5_share > TOP5_CONCENTRATED_THRESHOLD
             else "distributed_many_small_movers")

    return {
        "n_accounts_with_losses": len(deltas),
        "total_loss_usd": round(total_loss, 2),
        "top1_account_id": deltas[0]["account_id"],
        "top1_loss_usd": round(-deltas[0]["delta"], 2),
        "top1_share_of_total_loss": round(top1_share, 3),
        "top5_share_of_total_loss": round(top5_share, 3),
        "shape": shape,
        "flagged_account_ids": [d["account_id"] for d in deltas[:10]],
        "stop_here": single_dominant,
    }


# ---------------------------------------------------------------------
# Account-level usage-drop check (question 1's stage 4 rule)
# ---------------------------------------------------------------------
# FIXED after finding the same channel-mixing bug as the pricing check:
# summing raw `units` across an account's channels (email counts in the
# hundreds of thousands, sms in the thousands, voice in the tens of
# thousands) means a normal shift in which channel the account uses most
# swings the "total units" number wildly even when nothing real changed
# -- confirmed on account 1, whose raw units dropped 69% July->August
# while its actual revenue moved -1.0%. Every active account uses
# multiple channels (254/254 checked), so this wasn't an edge case, it
# was the default failure mode. Revenue is the fix: price-per-unit is
# fixed within each channel, so a revenue change IS the usage change on
# a per-channel basis, and dollars (unlike raw unit counts) are
# genuinely additive across an account's channels.

USAGE_DROP_THRESHOLD_PCT = 20.0


def account_revenue_series(conn, account_id: int, start_month: str, end_month: str):
    rows = conn.execute("""
        SELECT month, SUM(revenue) AS revenue FROM monthly_usage
        WHERE account_id = ? AND month >= ? AND month <= ?
        GROUP BY month ORDER BY month
    """, (account_id, start_month, end_month)).fetchall()
    return [(r["month"], r["revenue"]) for r in rows]


def account_had_usage_drop(conn, account_id: int, start_month: str, end_month: str) -> bool:
    """True if any month-over-month step in the window drops revenue
    (the valid, additive-across-channels proxy for usage) by
    >= USAGE_DROP_THRESHOLD_PCT."""
    series = account_revenue_series(conn, account_id, start_month, end_month)
    for (_, prev_r), (_, cur_r) in zip(series, series[1:]):
        if prev_r and prev_r > 0 and (prev_r - cur_r) / prev_r * 100 >= USAGE_DROP_THRESHOLD_PCT:
            return True
    return False


# Kept as an alias so any older caller expecting the units-based name
# still works, but it now points at the corrected revenue-based series.
account_usage_series = account_revenue_series


# account_monthly_revenue was a duplicate of account_revenue_series
# (defined above) -- removed rather than kept as a second copy of the
# same query.
account_monthly_revenue = account_revenue_series


# ---------------------------------------------------------------------
# NRR/GRR and ARR-waterfall helpers, matching analytics_server.py's
# `retention` and `arr_waterfall` tool definitions exactly (same cohort
# rule, same capped-GRR formula) so a playbook's numbers always agree
# with what Explorer/Investigator's MCP tools would report for the same
# window.
# ---------------------------------------------------------------------

def _retention_cohort(conn, base_month: str, current_month: str):
    base = conn.execute("""
        SELECT u.account_id, a.segment, SUM(u.revenue) AS base_rev
        FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
        WHERE u.month = ? GROUP BY u.account_id
    """, (base_month,)).fetchall()
    cur = {r["account_id"]: r["rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS rev FROM monthly_usage WHERE month = ? GROUP BY account_id",
        (current_month,)
    )}
    return base, cur


def retention_by_segment(conn, base_month: str, current_month: str) -> dict:
    """{segment: {n, base_total, nrr_pct, grr_pct}}, plus a "blended" key."""
    base, cur = _retention_cohort(conn, base_month, current_month)
    by_segment = {}
    for r in base:
        by_segment.setdefault(r["segment"], []).append(r)

    def summarize(rows):
        base_total = sum(r["base_rev"] for r in rows)
        if base_total == 0:
            return None
        cur_total = sum(cur.get(r["account_id"], 0.0) for r in rows)
        capped_total = sum(min(r["base_rev"], cur.get(r["account_id"], 0.0)) for r in rows)
        return {
            "n": len(rows), "base_total": round(base_total, 2),
            "nrr_pct": round(cur_total / base_total * 100, 1),
            "grr_pct": round(capped_total / base_total * 100, 1),
        }

    result = {seg: summarize(rows) for seg, rows in by_segment.items()}
    result["blended"] = summarize(base)
    return result


def arr_waterfall_buckets(conn, month: str) -> dict:
    prev_month = conn.execute(
        "SELECT strftime('%Y-%m', date(? || '-01', '-1 month')) AS m", (month,)
    ).fetchone()["m"]
    cur = {r["account_id"]: r["rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS rev FROM monthly_usage WHERE month=? GROUP BY account_id", (month,)
    )}
    prev = {r["account_id"]: r["rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS rev FROM monthly_usage WHERE month=? GROUP BY account_id", (prev_month,)
    )}
    new = expansion = contraction = churned = 0.0
    for acct, rev in cur.items():
        prev_rev = prev.get(acct, 0.0)
        if prev_rev == 0:
            new += rev
        elif rev > prev_rev:
            expansion += rev - prev_rev
        elif rev < prev_rev:
            contraction += prev_rev - rev
    for acct, prev_rev in prev.items():
        if acct not in cur:
            churned += prev_rev
    return {
        "month": month, "prev_month": prev_month,
        "new": round(new, 2), "expansion": round(expansion, 2),
        "contraction": round(contraction, 2), "churned": round(churned, 2),
        "net_new": round(new + expansion - contraction - churned, 2),
    }


# ---------------------------------------------------------------------
# Common-attribute gatherer for synthesis stages
# ---------------------------------------------------------------------

def gather_common_attributes(conn, account_ids: list) -> dict:
    if not account_ids:
        return {}
    placeholders = ",".join("?" for _ in account_ids)
    channels = conn.execute(f"""
        SELECT primary_channel, COUNT(*) AS n FROM accounts
        WHERE account_id IN ({placeholders}) GROUP BY primary_channel ORDER BY n DESC
    """, account_ids).fetchall()
    tickets = conn.execute(f"""
        SELECT category, COUNT(*) AS n FROM support_tickets
        WHERE account_id IN ({placeholders}) GROUP BY category ORDER BY n DESC
    """, account_ids).fetchall()
    return {
        "primary_channel_breakdown": {r["primary_channel"]: r["n"] for r in channels},
        "ticket_category_breakdown": {r["category"]: r["n"] for r in tickets},
    }


def claude_synthesize(evidence: dict, instruction: str) -> dict:
    """Bounded synthesis call shared by every question's final stage: no
    tools, no queries, reasons only over the evidence dict it's handed."""
    import json

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ran": False, "reason": "ANTHROPIC_API_KEY not set -- only the synthesis stage needs it"}

    from anthropic import Anthropic
    client = Anthropic()
    prompt = (
        f"{instruction} Do not ask for more data or invent numbers -- "
        "synthesize ONLY from the evidence below, citing the specific figures "
        "that support your answer.\n\nEvidence:\n"
        + json.dumps(evidence, indent=2, default=str)
    )
    response = client.messages.create(model=MODEL, max_tokens=512, messages=[{"role": "user", "content": prompt}])
    answer = "".join(b.text for b in response.content if b.type == "text")
    return {"ran": True, "answer": answer}
