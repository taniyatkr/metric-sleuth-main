"""
Question 2: What's driving the Mid-Market segment's underperformance?

Locked stage order, straight from your own analyst checklist -- "region"
was swapped for "source or rep concentration" since this dataset has no
region field (confirmed together before building this):

  1. Outlier check (is one big deal skewing the ARR comparison, in either
     direction)                                                -- diagnostic
  2. Seasonality check (does this calendar quarter dip most years anyway)
                                                                  -- diagnostic
  3. Source/rep concentration (in place of region)              -- diagnostic
  4. ACV check (did average deal size drop or hold steady)      -- diagnostic
  5. Deal volume / SQO check (did we generate fewer opportunities)
                                                                  -- diagnostic
  6. Win rate check (did the win rate itself drop)               -- diagnostic
  7. Landing-skew check (is open pipeline aging/piling up instead of
     closing, i.e. quietly sliding into next quarter)           -- diagnostic
  8. Root-cause synthesis (Claude, bounded)

Target window: 2026-Q2 (Apr-Jun), the quarter Mid-Market's closed-won ARR
bottomed out (see run_underperformance_playbook's defaults), compared
against 2026-Q1 (immediately prior) and 2025-Q2 (same quarter, prior
year). Every stage below was run against the real data before being
locked in here -- the headline finding (stage 6) is real: Mid-Market
closed just 1 of 34 deals in 2026-Q2 (a 2.9% win rate, vs. ~19-23% in the
comparison quarters), while pipeline generation, ACV, and open-pipeline
aging all came back clean. That's also why stage 1's outlier check
matters so much here: with only 1 win in the quarter, ANY dollar-based
view of "underperformance" is a sample-size-of-one story dressed up as a
trend -- the win-rate view (stage 6, which counts deals rather than
dollars) is the one that actually holds up.

Run directly: python3 q2_mid_market_underperformance.py
"""

from playbook_lib import connect, claude_synthesize

TARGET_SEGMENT = "Mid-Market"

OUTLIER_SHARE_THRESHOLD = 0.50          # top-1 deal >= 50% of the quarter's
                                         # closed-won ARR means the dollar
                                         # figure is effectively an n-of-1
                                         # or n-of-2 story, not a trend
SEASONALITY_MATCH_TOLERANCE_PCT = 5.0
CONCENTRATION_SHARE_THRESHOLD = 0.60    # matches the threshold used
                                         # elsewhere in this project (q3, q8)
ACV_DROP_THRESHOLD_PCT = 15.0
DEAL_VOLUME_DROP_THRESHOLD_PCT = 15.0
WIN_RATE_DROP_THRESHOLD_PTS = 8.0
PIPELINE_AGING_DAYS = 90                # older than one full quarter
PIPELINE_AGING_SHARE_THRESHOLD = 0.30


def _quarter_bounds(year: int, q: int):
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 3
    start = f"{year}-{start_month:02d}-01"
    end_year, end_mo = (year, end_month) if end_month <= 12 else (year + 1, 1)
    end = f"{end_year}-{end_mo:02d}-01"
    return start, end


# ---------------------------------------------------------------------
# Stage 1: Outlier check
# ---------------------------------------------------------------------
# RULE: for each quarter being compared, what share of that quarter's
# closed-won ARR came from its single largest deal? A share at or above
# OUTLIER_SHARE_THRESHOLD flags that quarter's dollar total as unreliable
# for trend-reading -- it doesn't stop the investigation (the underlying
# "why" still needs an answer), it just tells every later stage whether
# to trust the ARR number or lean on a count-based metric instead.

def _quarter_won_deals(conn, segment: str, start: str, end: str) -> list:
    rows = conn.execute("""
        SELECT closed_amount FROM opportunities
        WHERE segment = ? AND stage = 'Closed Won' AND close_date >= ? AND close_date < ?
        ORDER BY closed_amount DESC
    """, (segment, start, end)).fetchall()
    return [r["closed_amount"] for r in rows]


def stage_1_outlier_check(conn, segment: str, quarters: dict) -> dict:
    """quarters: {label: (start, end)}"""
    by_quarter = {}
    for label, (start, end) in quarters.items():
        amounts = _quarter_won_deals(conn, segment, start, end)
        total = sum(amounts)
        top1_share = (amounts[0] / total) if total else None
        by_quarter[label] = {
            "n_won": len(amounts),
            "total_arr": round(total, 2),
            "top1_deal_arr": amounts[0] if amounts else None,
            "top1_share_of_arr": round(top1_share, 3) if top1_share is not None else None,
            "outlier_driven": top1_share is not None and top1_share >= OUTLIER_SHARE_THRESHOLD,
        }
    any_outlier_driven = any(v["outlier_driven"] for v in by_quarter.values())
    return {
        "stage": "1_outlier_check",
        "by_quarter": by_quarter,
        "any_quarter_outlier_driven": any_outlier_driven,
        "note": ("if the target quarter is outlier_driven, its ARR total is really an n-of-1 "
                 "story -- prefer the count-based win rate (stage 6) over dollar comparisons "
                 "for reading what actually happened this quarter."),
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 2: Seasonality check
# ---------------------------------------------------------------------
# RULE: compare the target quarter's win rate (count-based, not ARR --
# see stage 1) to the same calendar quarter in the prior year. If the
# prior year's win rate for that same quarter was ALSO weak (within
# SEASONALITY_MATCH_TOLERANCE_PCT of this year's), this is a recurring
# seasonal pattern, not a new problem -- flag and stop.

def _quarter_win_rate(conn, segment: str, start: str, end: str) -> dict:
    row = conn.execute("""
        SELECT SUM(is_won) AS won, COUNT(*) AS closed FROM opportunities
        WHERE segment = ? AND stage IN ('Closed Won', 'Closed Lost')
          AND close_date >= ? AND close_date < ?
    """, (segment, start, end)).fetchone()
    if not row["closed"]:
        return {"won": 0, "closed": 0, "win_rate_pct": None}
    return {"won": row["won"], "closed": row["closed"], "win_rate_pct": round(row["won"] / row["closed"] * 100, 1)}


def stage_2_seasonality_check(conn, segment: str, target_bounds, same_quarter_last_year_bounds) -> dict:
    target = _quarter_win_rate(conn, segment, *target_bounds)
    last_year = _quarter_win_rate(conn, segment, *same_quarter_last_year_bounds)

    is_seasonal = (
        target["win_rate_pct"] is not None and last_year["win_rate_pct"] is not None
        and last_year["win_rate_pct"] <= target["win_rate_pct"] + SEASONALITY_MATCH_TOLERANCE_PCT
    )
    return {
        "stage": "2_seasonality_check",
        "target_quarter": target,
        "same_quarter_last_year": last_year,
        "is_seasonal": is_seasonal,
        "stop_here": is_seasonal,
    }


# ---------------------------------------------------------------------
# Stage 3: Source/rep concentration (in place of region)
# ---------------------------------------------------------------------
# RULE: of the target quarter's LOST deals, is more than
# CONCENTRATION_SHARE_THRESHOLD coming from a single source, or a single
# rep? If so, this points at one channel or one person rather than a
# segment-wide problem.

def stage_3_source_rep_concentration(conn, segment: str, start: str, end: str) -> dict:
    def concentration(dim: str):
        rows = conn.execute(f"""
            SELECT {dim} AS key, COUNT(*) AS n FROM opportunities
            WHERE segment = ? AND stage = 'Closed Lost' AND close_date >= ? AND close_date < ?
            GROUP BY {dim} ORDER BY n DESC
        """, (segment, start, end)).fetchall()
        total = sum(r["n"] for r in rows)
        breakdown = {r["key"]: r["n"] for r in rows}
        top_key, top_n = (rows[0]["key"], rows[0]["n"]) if rows else (None, 0)
        top_share = top_n / total if total else 0.0
        return {
            "breakdown": breakdown, "top": top_key, "top_share": round(top_share, 3),
            "concentrated": top_share >= CONCENTRATION_SHARE_THRESHOLD,
        }

    by_source = concentration("source")
    by_rep = concentration("rep")
    return {
        "stage": "3_source_rep_concentration",
        "by_source": by_source,
        "by_rep": by_rep,
        "concentrated_in_one_source": by_source["concentrated"],
        "concentrated_in_one_rep": by_rep["concentrated"],
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 4: ACV check
# ---------------------------------------------------------------------
# RULE: average closed-won deal size, target quarter vs. the prior
# quarter. A drop of ACV_DROP_THRESHOLD_PCT or more says deals themselves
# are shrinking; steady or higher ACV rules that out.

def _quarter_avg_acv(conn, segment: str, start: str, end: str) -> float | None:
    row = conn.execute("""
        SELECT AVG(closed_amount) AS acv FROM opportunities
        WHERE segment = ? AND stage = 'Closed Won' AND close_date >= ? AND close_date < ?
    """, (segment, start, end)).fetchone()
    return row["acv"]


def stage_4_acv_check(conn, segment: str, target_bounds, comparison_bounds) -> dict:
    target_acv = _quarter_avg_acv(conn, segment, *target_bounds)
    comparison_acv = _quarter_avg_acv(conn, segment, *comparison_bounds)

    change_pct = None
    acv_dropped = False
    if target_acv is not None and comparison_acv:
        change_pct = (target_acv - comparison_acv) / comparison_acv * 100
        acv_dropped = change_pct <= -ACV_DROP_THRESHOLD_PCT

    return {
        "stage": "4_acv_check",
        "target_quarter_avg_acv": round(target_acv, 2) if target_acv is not None else None,
        "comparison_quarter_avg_acv": round(comparison_acv, 2) if comparison_acv is not None else None,
        "change_pct": round(change_pct, 1) if change_pct is not None else None,
        "acv_dropped": acv_dropped,
        "caveat": "based on very few won deals in a single quarter -- see stage_1 before trusting this on its own",
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 5: Deal volume / SQO check
# ---------------------------------------------------------------------
# RULE: opportunities CREATED (a proxy for SQOs -- this dataset has no
# separate SQO flag) in the target quarter vs. the prior quarter. A drop
# of DEAL_VOLUME_DROP_THRESHOLD_PCT or more says the top of funnel
# shrank; flat or higher volume rules that out.

def _quarter_created_count(conn, segment: str, start: str, end: str) -> int:
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM opportunities
        WHERE segment = ? AND created_date >= ? AND created_date < ?
    """, (segment, start, end)).fetchone()
    return row["n"]


def stage_5_deal_volume_check(conn, segment: str, target_bounds, comparison_bounds) -> dict:
    target_n = _quarter_created_count(conn, segment, *target_bounds)
    comparison_n = _quarter_created_count(conn, segment, *comparison_bounds)

    change_pct = ((target_n - comparison_n) / comparison_n * 100) if comparison_n else None
    volume_dropped = change_pct is not None and change_pct <= -DEAL_VOLUME_DROP_THRESHOLD_PCT

    return {
        "stage": "5_deal_volume_check",
        "target_quarter_created": target_n,
        "comparison_quarter_created": comparison_n,
        "change_pct": round(change_pct, 1) if change_pct is not None else None,
        "volume_dropped": volume_dropped,
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 6: Win rate check
# ---------------------------------------------------------------------
# RULE: count-based win rate (won / (won+lost) by CLOSE date -- the
# reliable metric per stage 1, since it isn't dollar-weighted), target
# quarter vs. both comparison quarters. A drop of
# WIN_RATE_DROP_THRESHOLD_PTS or more against BOTH comparisons is the
# headline signal this playbook is built to catch.

def stage_6_win_rate_check(conn, segment: str, target_bounds, comparison_bounds, same_quarter_last_year_bounds) -> dict:
    target = _quarter_win_rate(conn, segment, *target_bounds)
    comparison = _quarter_win_rate(conn, segment, *comparison_bounds)
    last_year = _quarter_win_rate(conn, segment, *same_quarter_last_year_bounds)

    drop_vs_comparison = (
        round(comparison["win_rate_pct"] - target["win_rate_pct"], 1)
        if target["win_rate_pct"] is not None and comparison["win_rate_pct"] is not None else None
    )
    drop_vs_last_year = (
        round(last_year["win_rate_pct"] - target["win_rate_pct"], 1)
        if target["win_rate_pct"] is not None and last_year["win_rate_pct"] is not None else None
    )
    win_rate_collapsed = (
        drop_vs_comparison is not None and drop_vs_comparison >= WIN_RATE_DROP_THRESHOLD_PTS
        and drop_vs_last_year is not None and drop_vs_last_year >= WIN_RATE_DROP_THRESHOLD_PTS
    )

    return {
        "stage": "6_win_rate_check",
        "target_quarter": target,
        "comparison_quarter": comparison,
        "same_quarter_last_year": last_year,
        "drop_pts_vs_comparison_quarter": drop_vs_comparison,
        "drop_pts_vs_same_quarter_last_year": drop_vs_last_year,
        "win_rate_collapsed": win_rate_collapsed,
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 7: Landing-skew check
# ---------------------------------------------------------------------
# RULE: of the segment's CURRENTLY open pipeline, what share is older
# than PIPELINE_AGING_DAYS (one full quarter)? A high share means deals
# are piling up rather than closing -- quietly sliding into "next
# quarter" instead of resolving, which would make a later quarter look
# like a rebound that's really just this quarter's backlog clearing.

def stage_7_landing_skew_check(conn, segment: str, as_of_date: str) -> dict:
    rows = conn.execute("""
        SELECT created_date FROM opportunities WHERE segment = ? AND is_won IS NULL
    """, (segment,)).fetchall()
    ages_days = [
        (__import__("datetime").date.fromisoformat(as_of_date) - __import__("datetime").date.fromisoformat(r["created_date"])).days
        for r in rows
    ]
    n_aging = sum(1 for a in ages_days if a > PIPELINE_AGING_DAYS)
    share_aging = n_aging / len(ages_days) if ages_days else 0.0

    return {
        "stage": "7_landing_skew_check",
        "n_open": len(ages_days),
        "n_older_than_one_quarter": n_aging,
        "share_aging": round(share_aging, 3),
        "landing_skew_detected": share_aging >= PIPELINE_AGING_SHARE_THRESHOLD,
        "stop_here": False,
    }


def run_underperformance_playbook(
    segment=TARGET_SEGMENT,
    target_quarter=(2026, 2),
    comparison_quarter=(2026, 1),
    same_quarter_last_year=(2025, 2),
    as_of_date="2026-09-01",
):
    conn = connect()
    evidence = {}

    target_bounds = _quarter_bounds(*target_quarter)
    comparison_bounds = _quarter_bounds(*comparison_quarter)
    last_year_bounds = _quarter_bounds(*same_quarter_last_year)

    s1 = stage_1_outlier_check(conn, segment, {
        "target_quarter": target_bounds, "comparison_quarter": comparison_bounds,
        "same_quarter_last_year": last_year_bounds,
    })
    evidence["stage_1"] = s1
    print("Stage 1 -- Outlier check:")
    for label, v in s1["by_quarter"].items():
        print(f"  {label}: n_won={v['n_won']}, total_arr=${v['total_arr']:,}, "
              f"top1_share={v['top1_share_of_arr']}, outlier_driven={v['outlier_driven']}")

    s2 = stage_2_seasonality_check(conn, segment, target_bounds, last_year_bounds)
    evidence["stage_2"] = s2
    print("\nStage 2 -- Seasonality check:")
    print(f"  target quarter win rate: {s2['target_quarter']['win_rate_pct']}%, "
          f"same quarter last year: {s2['same_quarter_last_year']['win_rate_pct']}%")
    print(f"  is_seasonal: {s2['is_seasonal']}")
    if s2["stop_here"]:
        print("\n  -> STOP: this quarter is weak most years -- not a new problem.")
        conn.close()
        return evidence

    s3 = stage_3_source_rep_concentration(conn, segment, *target_bounds)
    evidence["stage_3"] = s3
    print("\nStage 3 -- Source/rep concentration (of the quarter's losses):")
    print(f"  by source: {s3['by_source']['breakdown']} (top: {s3['by_source']['top']}, "
          f"{s3['by_source']['top_share']:.1%}, concentrated={s3['concentrated_in_one_source']})")
    print(f"  by rep (top rep): {s3['by_rep']['top']} with {s3['by_rep']['breakdown'].get(s3['by_rep']['top'])} losses "
          f"({s3['by_rep']['top_share']:.1%}, concentrated={s3['concentrated_in_one_rep']})")

    s4 = stage_4_acv_check(conn, segment, target_bounds, comparison_bounds)
    evidence["stage_4"] = s4
    print("\nStage 4 -- ACV check:")
    print(f"  target avg ACV: ${s4['target_quarter_avg_acv']:,}" if s4["target_quarter_avg_acv"] else "  no won deals")
    print(f"  comparison avg ACV: ${s4['comparison_quarter_avg_acv']:,}, change: {s4['change_pct']}%, "
          f"acv_dropped: {s4['acv_dropped']}")

    s5 = stage_5_deal_volume_check(conn, segment, target_bounds, comparison_bounds)
    evidence["stage_5"] = s5
    print("\nStage 5 -- Deal volume / SQO check:")
    print(f"  target created: {s5['target_quarter_created']}, comparison created: {s5['comparison_quarter_created']}, "
          f"change: {s5['change_pct']}%, volume_dropped: {s5['volume_dropped']}")

    s6 = stage_6_win_rate_check(conn, segment, target_bounds, comparison_bounds, last_year_bounds)
    evidence["stage_6"] = s6
    print("\nStage 6 -- Win rate check:")
    print(f"  target: {s6['target_quarter']['win_rate_pct']}% ({s6['target_quarter']['won']}/{s6['target_quarter']['closed']})")
    print(f"  comparison quarter: {s6['comparison_quarter']['win_rate_pct']}%, "
          f"same quarter last year: {s6['same_quarter_last_year']['win_rate_pct']}%")
    print(f"  drop vs comparison: {s6['drop_pts_vs_comparison_quarter']} pts, "
          f"drop vs last year: {s6['drop_pts_vs_same_quarter_last_year']} pts")
    print(f"  win_rate_collapsed: {s6['win_rate_collapsed']}")

    s7 = stage_7_landing_skew_check(conn, segment, as_of_date)
    evidence["stage_7"] = s7
    print("\nStage 7 -- Landing-skew check (open pipeline aging):")
    print(f"  {s7['n_older_than_one_quarter']}/{s7['n_open']} open deals older than {PIPELINE_AGING_DAYS} days "
          f"({s7['share_aging']:.1%}), landing_skew_detected: {s7['landing_skew_detected']}")

    s8 = claude_synthesize(
        evidence,
        "You are given evidence a fixed analytics pipeline collected while investigating the "
        "Mid-Market segment's underperformance. Pay attention to stage_1: if the target "
        "quarter is outlier_driven (very few won deals), treat stage_6's count-based win "
        "rate as the reliable signal and be cautious about leaning on dollar totals or ACV "
        "(stage_4) from a near-single-digit sample. State the most likely root cause in 2-3 "
        "sentences, citing the specific figures that support it, and say plainly if several "
        "stages came back clean (e.g. deal volume, ACV, source/rep concentration, landing "
        "skew) so the reader knows what was ruled out, not just what was found.",
    )
    evidence["stage_8"] = s8
    print("\nStage 8 -- Root-cause synthesis:")
    print(f"  {s8['answer']}" if s8["ran"] else f"  (skipped: {s8['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_underperformance_playbook()
