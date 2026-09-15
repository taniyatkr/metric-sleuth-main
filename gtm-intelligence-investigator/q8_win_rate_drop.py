"""
Question 8: Why is our win rate lower this quarter than last?

Locked stage order (stage 5 adjusted from the original plan -- the
schema has no record of which funnel stage a lost deal died at, every
Closed Lost row just says "Closed Lost"; swapped for loss timing and
pre/post-qualification loss share, which the data actually supports):

  1. Confirm the drop is real, check seasonality (same quarter last year)
                                                                    -- gate
  2. Segment concentration                                        -- diagnostic
  3. Source/rep concentration                                     -- diagnostic
  4. Mix-shift check (Simpson's paradox: did the pipeline mix simply
     shift toward segments/sources that convert worse anyway, rather
     than anything getting worse)                                 -- diagnostic
  5. Loss-timing check (days-to-loss, pre- vs post-qualification loss share)
                                                                    -- diagnostic
  6. Root-cause synthesis (Claude, bounded)

Run directly: python3 q8_win_rate_drop.py
"""

from playbook_lib import connect, claude_synthesize

MIN_MEANINGFUL_DROP_PTS = 3.0
CONCENTRATION_SHARE_THRESHOLD = 0.60
MIX_SHIFT_DOMINANT_SHARE = 0.50


def _quarter_bounds(year: int, q: int):
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 3
    start = f"{year}-{start_month:02d}-01"
    end_year, end_mo = (year, end_month) if end_month <= 12 else (year + 1, 1)
    end = f"{end_year}-{end_mo:02d}-01"
    return start, end


def win_rate(conn, start: str, end: str, dim: str | None = None) -> dict:
    """Win rate among CLOSED deals (won or lost) with close_date in
    [start, end). dim groups by 'segment', 'source', or 'rep'; None gives
    the blended rate."""
    if dim:
        rows = conn.execute(f"""
            SELECT {dim} AS key, SUM(is_won) AS won, COUNT(*) AS closed
            FROM opportunities WHERE stage IN ('Closed Won','Closed Lost')
              AND close_date >= ? AND close_date < ?
            GROUP BY {dim}
        """, (start, end)).fetchall()
        return {r["key"]: {"won": r["won"], "closed": r["closed"], "win_rate_pct": round(r["won"] / r["closed"] * 100, 1)}
                for r in rows if r["closed"]}
    row = conn.execute("""
        SELECT SUM(is_won) AS won, COUNT(*) AS closed FROM opportunities
        WHERE stage IN ('Closed Won','Closed Lost') AND close_date >= ? AND close_date < ?
    """, (start, end)).fetchone()
    if not row["closed"]:
        return {"won": 0, "closed": 0, "win_rate_pct": None}
    return {"won": row["won"], "closed": row["closed"], "win_rate_pct": round(row["won"] / row["closed"] * 100, 1)}


def stage_1_confirm_drop(conn, this_q_bounds, last_q_bounds, same_q_last_year_bounds) -> dict:
    this_q = win_rate(conn, *this_q_bounds)
    last_q = win_rate(conn, *last_q_bounds)
    same_q_ly = win_rate(conn, *same_q_last_year_bounds)

    drop_pts = (last_q["win_rate_pct"] - this_q["win_rate_pct"]) if this_q["win_rate_pct"] is not None and last_q["win_rate_pct"] is not None else None
    is_meaningful_drop = drop_pts is not None and drop_pts >= MIN_MEANINGFUL_DROP_PTS
    looks_seasonal = (same_q_ly["win_rate_pct"] is not None and this_q["win_rate_pct"] is not None
                       and same_q_ly["win_rate_pct"] <= this_q["win_rate_pct"] + MIN_MEANINGFUL_DROP_PTS)

    return {
        "stage": "1_confirm_drop",
        "this_quarter": this_q, "last_quarter": last_q, "same_quarter_last_year": same_q_ly,
        "drop_pts": round(drop_pts, 1) if drop_pts is not None else None,
        "is_meaningful_drop": is_meaningful_drop,
        "looks_seasonal": looks_seasonal,
        "stop_here": not is_meaningful_drop,
    }


def stage_2_and_3_concentration(conn, this_q_bounds, last_q_bounds, dim: str) -> dict:
    this_q = win_rate(conn, *this_q_bounds, dim=dim)
    last_q = win_rate(conn, *last_q_bounds, dim=dim)
    drops = {k: round(last_q[k]["win_rate_pct"] - this_q[k]["win_rate_pct"], 1)
             for k in this_q if k in last_q}
    worst = max(drops, key=drops.get) if drops else None
    return {"dim": dim, "this_quarter": this_q, "last_quarter": last_q, "drop_pts_by_key": drops, "worst": worst, "stop_here": False}


def stage_4_mix_shift(conn, this_q_bounds, last_q_bounds) -> dict:
    this_q_by_seg = win_rate(conn, *this_q_bounds, dim="segment")
    last_q_by_seg = win_rate(conn, *last_q_bounds, dim="segment")
    last_q_blended = win_rate(conn, *last_q_bounds)
    this_q_blended = win_rate(conn, *this_q_bounds)

    this_q_total_closed = sum(v["closed"] for v in this_q_by_seg.values())
    mix_adjusted_rate = None
    if this_q_total_closed and all(seg in last_q_by_seg for seg in this_q_by_seg):
        mix_adjusted_rate = sum(
            (v["closed"] / this_q_total_closed) * last_q_by_seg[seg]["win_rate_pct"]
            for seg, v in this_q_by_seg.items()
        )

    mix_share_of_drop = None
    mix_shift_dominant = False
    if mix_adjusted_rate is not None and last_q_blended["win_rate_pct"] is not None and this_q_blended["win_rate_pct"] is not None:
        total_drop = last_q_blended["win_rate_pct"] - this_q_blended["win_rate_pct"]
        mix_explained = last_q_blended["win_rate_pct"] - mix_adjusted_rate
        if total_drop:
            mix_share_of_drop = mix_explained / total_drop
            mix_shift_dominant = mix_share_of_drop >= MIX_SHIFT_DOMINANT_SHARE

    return {
        "stage": "4_mix_shift",
        "last_quarter_blended_pct": last_q_blended["win_rate_pct"],
        "this_quarter_blended_pct": this_q_blended["win_rate_pct"],
        "mix_adjusted_rate_pct": round(mix_adjusted_rate, 1) if mix_adjusted_rate is not None else None,
        "mix_share_of_drop": round(mix_share_of_drop, 2) if mix_share_of_drop is not None else None,
        "mix_shift_dominant": mix_shift_dominant,
        "stop_here": False,
    }


def stage_5_loss_timing(conn, this_q_bounds, last_q_bounds) -> dict:
    def timing(start, end):
        rows = conn.execute("""
            SELECT created_date, qualified_date, close_date FROM opportunities
            WHERE stage = 'Closed Lost' AND close_date >= ? AND close_date < ?
        """, (start, end)).fetchall()
        if not rows:
            return {"n": 0, "avg_days_to_loss": None, "pct_lost_pre_qualification": None}
        import datetime
        days = [(datetime.date.fromisoformat(r["close_date"]) - datetime.date.fromisoformat(r["created_date"])).days for r in rows]
        pre_qual = sum(1 for r in rows if r["qualified_date"] is None)
        return {
            "n": len(rows),
            "avg_days_to_loss": round(sum(days) / len(days), 1),
            "pct_lost_pre_qualification": round(pre_qual / len(rows) * 100, 1),
        }

    return {"stage": "5_loss_timing", "this_quarter": timing(*this_q_bounds), "last_quarter": timing(*last_q_bounds), "stop_here": False}


def run_win_rate_drop_playbook(this_q=(2026, 3), last_q=(2026, 2), same_q_last_year=(2025, 3)):
    conn = connect()
    evidence = {}
    this_q_bounds = _quarter_bounds(*this_q)
    last_q_bounds = _quarter_bounds(*last_q)
    same_q_ly_bounds = _quarter_bounds(*same_q_last_year)

    s1 = stage_1_confirm_drop(conn, this_q_bounds, last_q_bounds, same_q_ly_bounds)
    evidence["stage_1"] = s1
    print("Stage 1 -- Confirm the drop:")
    print(f"  this quarter: {s1['this_quarter']}, last quarter: {s1['last_quarter']}")
    print(f"  drop: {s1['drop_pts']} pts, is_meaningful_drop: {s1['is_meaningful_drop']}, looks_seasonal: {s1['looks_seasonal']}")
    if s1["stop_here"]:
        print("\n  -> STOP: drop isn't meaningful, nothing to investigate.")
        conn.close()
        return evidence

    s2 = stage_2_and_3_concentration(conn, this_q_bounds, last_q_bounds, "segment")
    evidence["stage_2"] = s2
    print(f"\nStage 2 -- Segment concentration: drop_pts_by_key={s2['drop_pts_by_key']}, worst={s2['worst']}")

    s3 = stage_2_and_3_concentration(conn, this_q_bounds, last_q_bounds, "source")
    evidence["stage_3"] = s3
    print(f"\nStage 3 -- Source concentration: drop_pts_by_key={s3['drop_pts_by_key']}, worst={s3['worst']}")

    s4 = stage_4_mix_shift(conn, this_q_bounds, last_q_bounds)
    evidence["stage_4"] = s4
    print(f"\nStage 4 -- Mix-shift check: last_q={s4['last_quarter_blended_pct']}%, this_q={s4['this_quarter_blended_pct']}%, "
          f"mix_adjusted={s4['mix_adjusted_rate_pct']}%, mix_share_of_drop={s4['mix_share_of_drop']}, dominant={s4['mix_shift_dominant']}")

    s5 = stage_5_loss_timing(conn, this_q_bounds, last_q_bounds)
    evidence["stage_5"] = s5
    print(f"\nStage 5 -- Loss timing: this_q={s5['this_quarter']}, last_q={s5['last_quarter']}")

    s6 = claude_synthesize(
        evidence,
        "You are given evidence a fixed analytics pipeline collected while investigating "
        "a win-rate drop. Pay special attention to stage_4: if mix_shift_dominant is True, "
        "most of the drop is a mix-shift artifact, not genuine performance degradation -- "
        "say so plainly rather than implying reps or deal quality got worse. State the most "
        "likely root cause in 2-3 sentences.",
    )
    evidence["stage_6"] = s6
    print("\nStage 6 -- Synthesis:")
    print(f"  {s6['answer']}" if s6["ran"] else f"  (skipped: {s6['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_win_rate_drop_playbook()
