"""
GTM Intelligence Investigator -- retention-drop playbook (flagship question)

This is a deliberately different shape than investigator.py's open-ended
loop. Instead of handing Claude the tool list and letting it decide the
whole path, THIS pipeline follows the exact sequence an analyst defined:
each stage is fixed code with an explicit rule, run in order, with early
stops when a stage's rule is satisfied. Claude's role is scoped to stage
6 only (root-cause synthesis over evidence already gathered) -- it never
decides which stage runs next or what to query.

Locked stage order:

  1. Seasonality check                          -- code, fixed rule
  2. Concentration & pace                        -- code, fixed rule
  3. Segment comparison                          -- code, fixed rule
  4. Account-level usage-drop shape (>20% MoM)   -- code, fixed rule
  5. Segment-wide usage trend (pricing vs usage) -- code, fixed rule
  6. Root-cause / common-attribute synthesis     -- Claude, bounded to
     synthesizing the evidence dict stages 1-5 + gather_common_attributes()
     already produced -- it gets no tools and cannot query anything itself

Run directly to see the full pipeline against the real Mid-Market
Sep'25->Sep'26 retention drop:  python3 retention_drop_playbook.py
(stage 6 needs ANTHROPIC_API_KEY in the environment; stages 1-5 do not)
"""

import json
import os
import sqlite3

MODEL = "claude-sonnet-5"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "metric_sleuth.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------
# Stage 1: Seasonality check
# ---------------------------------------------------------------------
# RULE (proposed, confirm the threshold): for the calendar month where the
# drop is concentrated, compute this segment's month-over-month revenue
# growth rate for that same calendar month in each prior year available.
# If at least 2 of the prior years also show a decline within 5 points of
# this year's, call it seasonal.

SEASONALITY_MATCH_TOLERANCE_PTS = 5.0
SEASONALITY_MIN_MATCHING_YEARS = 2


def segment_mom_growth(conn, segment: str, month: str) -> float | None:
    """Month-over-month revenue growth rate (%) for a segment, comparing
    `month` to the month before it. Returns None if either month has no
    data (e.g. before the segment had any accounts yet)."""
    prev_month = conn.execute(
        "SELECT strftime('%Y-%m', date(? || '-01', '-1 month')) AS m", (month,)
    ).fetchone()["m"]

    def total_revenue(m):
        row = conn.execute("""
            SELECT SUM(u.revenue) AS rev FROM monthly_usage u
            JOIN accounts a ON a.account_id = u.account_id
            WHERE a.segment = ? AND u.month = ?
        """, (segment, m)).fetchone()
        return row["rev"]

    cur, prev = total_revenue(month), total_revenue(prev_month)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100


def stage_1_seasonality_check(conn, segment: str, drop_month: str) -> dict:
    """Checks whether drop_month's decline also happened in the same
    calendar month in prior years."""
    this_year_growth = segment_mom_growth(conn, segment, drop_month)
    month_num = drop_month.split("-")[1]
    year_num = int(drop_month.split("-")[0])

    prior_year_growths = {}
    for years_back in (1, 2):
        prior_month = f"{year_num - years_back}-{month_num}"
        g = segment_mom_growth(conn, segment, prior_month)
        if g is not None:
            prior_year_growths[prior_month] = g

    matches = [
        m for m, g in prior_year_growths.items()
        if this_year_growth is not None
        and g < 0
        and abs(g - this_year_growth) <= SEASONALITY_MATCH_TOLERANCE_PTS
    ]

    is_seasonal = len(matches) >= SEASONALITY_MIN_MATCHING_YEARS

    return {
        "stage": "1_seasonality",
        "this_year_growth_pct": this_year_growth,
        "prior_year_growths_pct": prior_year_growths,
        "matching_prior_years": matches,
        "is_seasonal": is_seasonal,
        "stop_here": is_seasonal,
    }


# ---------------------------------------------------------------------
# Stage 2: Concentration & pace check
# ---------------------------------------------------------------------
# RULE (proposed, confirm the thresholds):
#  - if the single largest contracting/churned account explains >40% of
#    total $ lost in the cohort, flag as "single dominant account" and stop
#  - otherwise, if the top 5 accounts explain >60% of total $ lost,
#    characterize as "concentrated" (a few big movers); else "distributed"
#    (many smaller movers) -- neither of these stops the investigation,
#    they just describe its shape for later stages

SINGLE_ACCOUNT_STOP_THRESHOLD = 0.40
TOP5_CONCENTRATED_THRESHOLD = 0.60


def stage_2_concentration_and_pace(conn, segment: str, base_month: str, current_month: str) -> dict:
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
    deltas.sort(key=lambda d: d["delta"])  # most negative first

    total_loss = sum(-d["delta"] for d in deltas)
    if total_loss == 0 or not deltas:
        return {"stage": "2_concentration_pace", "no_losses": True, "stop_here": False}

    top1_share = -deltas[0]["delta"] / total_loss
    top5_share = sum(-d["delta"] for d in deltas[:5]) / total_loss
    single_dominant = top1_share > SINGLE_ACCOUNT_STOP_THRESHOLD

    if single_dominant:
        shape = "single_dominant_account"
    elif top5_share > TOP5_CONCENTRATED_THRESHOLD:
        shape = "concentrated_few_big_movers"
    else:
        shape = "distributed_many_small_movers"

    return {
        "stage": "2_concentration_pace",
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
# Stage 3: Segment comparison
# ---------------------------------------------------------------------
# RULE (proposed, confirm the threshold): compute this segment's revenue
# % change from base_month to current_month, and the same for every other
# segment. If ALL other segments also show a decline within 10 points of
# this segment's, this isn't a Mid-Market story, it's company-wide --
# flag and stop (a different investigation, not this one).

SEGMENT_COMPARISON_TOLERANCE_PTS = 10.0


def segment_pct_change(conn, segment: str, base_month: str, current_month: str) -> float | None:
    def total_revenue(m):
        row = conn.execute("""
            SELECT SUM(u.revenue) AS rev FROM monthly_usage u
            JOIN accounts a ON a.account_id = u.account_id
            WHERE a.segment = ? AND u.month = ?
        """, (segment, m)).fetchone()
        return row["rev"]
    base, cur = total_revenue(base_month), total_revenue(current_month)
    if base is None or cur is None or base == 0:
        return None
    return (cur - base) / base * 100


def stage_3_segment_comparison(conn, target_segment: str, base_month: str, current_month: str) -> dict:
    all_segments = [r["segment"] for r in conn.execute("SELECT DISTINCT segment FROM accounts")]
    other_segments = [s for s in all_segments if s != target_segment]

    target_change = segment_pct_change(conn, target_segment, base_month, current_month)
    other_changes = {s: segment_pct_change(conn, s, base_month, current_month) for s in other_segments}

    comparable = [
        s for s, c in other_changes.items()
        if c is not None and target_change is not None
        and c < 0
        and abs(c - target_change) <= SEGMENT_COMPARISON_TOLERANCE_PTS
    ]
    is_company_wide = len(other_segments) > 0 and len(comparable) == len(other_segments)

    return {
        "stage": "3_segment_comparison",
        "target_segment": target_segment,
        "target_change_pct": target_change,
        "other_segment_changes_pct": other_changes,
        "comparably_declining_segments": comparable,
        "is_company_wide": is_company_wide,
        "stop_here": is_company_wide,
    }


# ---------------------------------------------------------------------
# Stage 4: Account-level usage-drop shape (>20% MoM)
# ---------------------------------------------------------------------
# RULE, straight from your own methodology: for each flagged account (the
# ones with losses from stage 2), check whether ANY month in the window
# shows a >=20% MoM drop in usage.
#
# CORRECTED (same bug family as stage 5): this originally summed raw
# `units` across an account's channels -- but email counts in the
# hundreds of thousands, sms in the thousands, voice in the tens of
# thousands, so a normal shift in which channel an account uses most
# swings "total units" wildly with no real change in usage. Confirmed on
# a real account: raw units dropped 69% month-over-month while its
# actual revenue moved -1.0%. Every active account uses multiple
# channels, so this wasn't rare. Fixed to check REVENUE instead --
# price-per-unit is fixed within each channel, so a revenue change IS
# the usage change per channel, and dollars (unlike raw unit counts) are
# genuinely additive across an account's channels.

USAGE_DROP_THRESHOLD_PCT = 20.0
USAGE_DROP_MAJORITY_THRESHOLD = 0.50


def account_revenue_series(conn, account_id: int, start_month: str, end_month: str):
    rows = conn.execute("""
        SELECT month, SUM(revenue) AS revenue FROM monthly_usage
        WHERE account_id = ? AND month >= ? AND month <= ?
        GROUP BY month ORDER BY month
    """, (account_id, start_month, end_month)).fetchall()
    return [(r["month"], r["revenue"]) for r in rows]


def account_had_usage_drop(conn, account_id: int, start_month: str, end_month: str) -> bool:
    series = account_revenue_series(conn, account_id, start_month, end_month)
    for (_, prev_r), (_, cur_r) in zip(series, series[1:]):
        if prev_r and prev_r > 0 and (prev_r - cur_r) / prev_r * 100 >= USAGE_DROP_THRESHOLD_PCT:
            return True
    return False


def stage_4_account_usage_drop_shape(conn, flagged_account_ids: list, base_month: str, current_month: str) -> dict:
    flags = {aid: account_had_usage_drop(conn, aid, base_month, current_month) for aid in flagged_account_ids}
    n_with_drop = sum(flags.values())
    share_with_drop = n_with_drop / len(flags) if flags else 0.0

    return {
        "stage": "4_account_usage_drop_shape",
        "n_flagged_accounts_checked": len(flags),
        "n_with_20pct_drop": n_with_drop,
        "share_with_20pct_drop": round(share_with_drop, 3),
        "usage_decline_precedes_revenue_loss": share_with_drop >= USAGE_DROP_MAJORITY_THRESHOLD,
        "accounts_with_drop": [aid for aid, had in flags.items() if had],
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 5: Channel-level usage trend (rate-stability check)
# ---------------------------------------------------------------------
# RULE, straight from your methodology ("this would point at a potential
# pricing and product related investigation"), CORRECTED after checking
# channel_rates: it's a static table -- list_price_usd never varies by
# time for any channel anywhere in this dataset, so there is no
# mechanism by which a real price change could exist here. An earlier
# version of this stage summed "units" across all four channels (sms
# messages + voice minutes + email sends + verification checks) into one
# number and compared it to total revenue -- but those aren't the same
# unit, so a shift in the MIX between channels moves that blended ratio
# with zero actual price change anywhere, which is exactly what produced
# a false "price increase" read the first time this ran. Fixed to
# compare units-vs-revenue per channel (the only apples-to-apples
# comparison) -- within a single channel, a gap would mean a genuine
# rate effect; since none exists in this dataset, expect none found, and
# the useful output is which channel is actually driving the decline.

def stage_5_channel_usage_trend(conn, segment: str, base_month: str, current_month: str) -> dict:
    channels = [r["channel"] for r in conn.execute("SELECT DISTINCT channel FROM monthly_usage")]
    per_channel = {}
    for ch in channels:
        base_row = conn.execute("""
            SELECT SUM(u.units) AS units, SUM(u.revenue) AS revenue
            FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
            WHERE a.segment = ? AND u.channel = ? AND u.month = ?
        """, (segment, ch, base_month)).fetchone()
        cur_row = conn.execute("""
            SELECT SUM(u.units) AS units, SUM(u.revenue) AS revenue
            FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
            WHERE a.segment = ? AND u.channel = ? AND u.month = ?
        """, (segment, ch, current_month)).fetchone()
        if not base_row["units"] or not base_row["revenue"]:
            continue
        units_change = (cur_row["units"] - base_row["units"]) / base_row["units"] * 100
        rev_change = (cur_row["revenue"] - base_row["revenue"]) / base_row["revenue"] * 100
        per_channel[ch] = {
            "units_change_pct": round(units_change, 1),
            "revenue_change_pct": round(rev_change, 1),
            "gap_pts": round(rev_change - units_change, 1),
        }

    RATE_EFFECT_GAP_PTS = 10.0
    rate_effect_detected = any(abs(c["gap_pts"]) >= RATE_EFFECT_GAP_PTS for c in per_channel.values())
    declining = {ch: c for ch, c in per_channel.items() if c["revenue_change_pct"] < 0}
    dominant_declining_channel = (
        min(declining, key=lambda ch: declining[ch]["revenue_change_pct"]) if declining else None
    )

    return {
        "stage": "5_channel_usage_trend",
        "per_channel": per_channel,
        "rate_effect_detected": rate_effect_detected,
        "dominant_declining_channel": dominant_declining_channel,
        "note": ("channel_rates is static in this dataset -- rate_effect_detected confirms "
                 "whether price is a factor rather than assuming it isn't. False here means "
                 "the decline is a pure usage/volume contraction, not a rate change."),
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 6: Root-cause / common-attribute synthesis -- Claude, bounded
# ---------------------------------------------------------------------
# This is the ONE stage where Claude is involved. It gets no tools and
# makes no queries of its own -- it is handed the evidence dict stages
# 1-5 already produced, plus a code-gathered breakdown of what the
# flagged accounts have in common (channel, ticket category), and is
# asked only to synthesize a root cause from what's already there.

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


def stage_6_root_cause_synthesis(conn, evidence: dict, flagged_account_ids: list) -> dict:
    common = gather_common_attributes(conn, flagged_account_ids)
    evidence_for_claude = {**evidence, "common_attributes": common}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "stage": "6_root_cause_synthesis",
            "ran": False,
            "reason": "ANTHROPIC_API_KEY not set -- stages 1-5 need no key, only this one does",
            "common_attributes": common,
        }

    from anthropic import Anthropic
    client = Anthropic()
    prompt = (
        "You are given evidence a fixed analytics pipeline already collected "
        "while investigating a segment's retention drop. Do not ask for more "
        "data or invent numbers -- synthesize ONLY from what is below.\n\n"
        "How to weigh it: stage_5 is segment-WIDE (every account in the "
        "segment, not just the ones actually driving the loss) and exists "
        "only to rule pricing in or out -- if rate_effect_detected is False, "
        "do not describe this as a price increase or price change, and do "
        "not treat stage_5's dominant_declining_channel as the cause, since "
        "it can reflect segment-wide noise unrelated to the accounts that "
        "are actually declining. common_attributes is scoped to the specific "
        "flagged/highest-loss accounts from stage_2 -- it is the more precise "
        "signal for which channel and which ticket category are actually "
        "implicated; treat its largest ticket_category_breakdown entry as the "
        "leading proximate-cause candidate. State the most likely root cause "
        "in 2-3 sentences, citing the specific figures that support it.\n\n"
        "Evidence:\n" + json.dumps(evidence_for_claude, indent=2, default=str)
    )
    response = client.messages.create(
        model=MODEL, max_tokens=512, messages=[{"role": "user", "content": prompt}],
    )
    answer = "".join(b.text for b in response.content if b.type == "text")
    return {"stage": "6_root_cause_synthesis", "ran": True, "common_attributes": common, "answer": answer}


def run_flagship_playbook(segment="Mid-Market", drop_month="2026-03",
                            base_month="2025-09", current_month="2026-09"):
    conn = _connect()
    evidence = {}

    s1 = stage_1_seasonality_check(conn, segment, drop_month)
    evidence["stage_1"] = s1
    print("Stage 1 -- Seasonality check:")
    print(f"  this year ({drop_month}) MoM growth: {s1['this_year_growth_pct']:.1f}%"
          if s1["this_year_growth_pct"] is not None else "  no data")
    print(f"  prior years, same month: {s1['prior_year_growths_pct']}")
    print(f"  is_seasonal: {s1['is_seasonal']}")
    if s1["stop_here"]:
        print("\n  -> STOP: pattern recurs historically, flagging as seasonal.")
        conn.close()
        return evidence

    s2 = stage_2_concentration_and_pace(conn, segment, base_month, current_month)
    evidence["stage_2"] = s2
    print("\nStage 2 -- Concentration & pace check:")
    print(f"  accounts with losses: {s2.get('n_accounts_with_losses')}")
    print(f"  total loss: ${s2.get('total_loss_usd'):,}" if not s2.get("no_losses") else "  no losses")
    print(f"  top 1 account (id={s2.get('top1_account_id')}): "
          f"${s2.get('top1_loss_usd'):,} = {s2.get('top1_share_of_total_loss', 0):.1%} of total loss")
    print(f"  top 5 accounts: {s2.get('top5_share_of_total_loss', 0):.1%} of total loss")
    print(f"  shape: {s2.get('shape')}")
    if s2["stop_here"]:
        print("\n  -> STOP: one account dominates the loss, flagging as an outlier, not systemic.")
        conn.close()
        return evidence

    s3 = stage_3_segment_comparison(conn, segment, base_month, current_month)
    evidence["stage_3"] = s3
    print("\nStage 3 -- Segment comparison:")
    print(f"  {segment} change: {s3['target_change_pct']:.1f}%" if s3["target_change_pct"] is not None else "  no data")
    print(f"  other segments: { {k: round(v, 1) if v is not None else None for k, v in s3['other_segment_changes_pct'].items()} }")
    print(f"  is_company_wide: {s3['is_company_wide']}")
    if s3["stop_here"]:
        print("\n  -> STOP: other segments show a comparable decline -- this is company-wide, not Mid-Market-specific.")
        conn.close()
        return evidence

    flagged_ids = s2.get("flagged_account_ids", [])

    s4 = stage_4_account_usage_drop_shape(conn, flagged_ids, base_month, current_month)
    evidence["stage_4"] = s4
    print("\nStage 4 -- Account-level usage-drop shape (>=20% MoM):")
    print(f"  {s4['n_with_20pct_drop']}/{s4['n_flagged_accounts_checked']} flagged accounts had a >=20% MoM usage drop")
    print(f"  usage_decline_precedes_revenue_loss: {s4['usage_decline_precedes_revenue_loss']}")

    s5 = stage_5_channel_usage_trend(conn, segment, base_month, current_month)
    evidence["stage_5"] = s5
    print("\nStage 5 -- Channel-level usage trend (rate-stability check):")
    for ch, c in s5["per_channel"].items():
        print(f"  {ch}: units {c['units_change_pct']:+.1f}%, revenue {c['revenue_change_pct']:+.1f}%, gap {c['gap_pts']:+.1f} pts")
    print(f"  rate_effect_detected: {s5['rate_effect_detected']}, dominant_declining_channel: {s5['dominant_declining_channel']}")

    s6 = stage_6_root_cause_synthesis(conn, evidence, flagged_ids)
    evidence["stage_6"] = s6
    print("\nStage 6 -- Root-cause / common-attribute synthesis:")
    print(f"  common attributes: {s6['common_attributes']}")
    if s6["ran"]:
        print(f"\n  {s6['answer']}")
    else:
        print(f"  (Claude synthesis skipped: {s6['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_flagship_playbook()
