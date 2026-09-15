"""
Question 9: Which source or rep is producing the best-quality pipeline?

Another ranking question -- no gates, no required Claude stage (an
optional narrative wrapper is included but the ranking itself is the
answer).

  1. Win rate by source and by rep
  2. Average ACV by source and by rep
  3. Average sales cycle length by source and by rep
  4. Composite quality ranking (win rate + ACV + cycle time)
  5. Optional Claude narrative summarizing the ranking

Run directly: python3 q9_source_rep_quality.py
"""

import datetime

from playbook_lib import connect, claude_synthesize

MIN_CLOSED_DEALS_FOR_REP_RANKING = 5  # reps with too few closed deals are
                                       # noisy to rank meaningfully


# ---------------------------------------------------------------------
# Stages 1-3: win rate, ACV, and cycle time -- by source, and by rep
# ---------------------------------------------------------------------
# RULE: one query, reused for both dimensions (dim='source' or
# dim='rep'), over every deal that's actually CLOSED (won or lost) --
# still-open deals have no outcome yet, so they're excluded rather than
# silently treated as losses. win_rate_pct, avg_acv (won deals only,
# since a lost deal has no closed_amount), and avg_cycle_days
# (created_date to close_date, won deals only) are the three raw
# ingredients stage 4's composite ranking combines.

def _quality_metrics(conn, dim: str) -> dict:
    rows = conn.execute(f"""
        SELECT {dim} AS key, opportunity_id, is_won, closed_amount, created_date, close_date
        FROM opportunities WHERE stage IN ('Closed Won', 'Closed Lost')
    """).fetchall()

    by_key = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)

    metrics = {}
    for key, deals in by_key.items():
        closed = len(deals)
        won_deals = [d for d in deals if d["is_won"]]
        won = len(won_deals)
        win_rate_pct = won / closed * 100 if closed else None
        avg_acv = sum(d["closed_amount"] for d in won_deals) / won if won else None
        cycle_days = [
            (datetime.date.fromisoformat(d["close_date"]) - datetime.date.fromisoformat(d["created_date"])).days
            for d in won_deals
        ]
        avg_cycle = sum(cycle_days) / len(cycle_days) if cycle_days else None
        metrics[key] = {
            "closed_deals": closed, "won_deals": won,
            "win_rate_pct": round(win_rate_pct, 1) if win_rate_pct is not None else None,
            "avg_acv": round(avg_acv, 2) if avg_acv is not None else None,
            "avg_cycle_days": round(avg_cycle, 1) if avg_cycle is not None else None,
        }
    return metrics


def _normalize(values: list) -> dict:
    """Min-max normalize a {key: value} dict to 0-1, higher-is-better."""
    vals = [v for v in values.values() if v is not None]
    if not vals or max(vals) == min(vals):
        return {k: 0.5 for k in values}
    lo, hi = min(vals), max(vals)
    return {k: ((v - lo) / (hi - lo) if v is not None else 0.5) for k, v in values.items()}


# ---------------------------------------------------------------------
# Stage 4: composite quality ranking
# ---------------------------------------------------------------------
# RULE: win rate, ACV, and cycle time are each on a different scale and
# a different direction (higher is better for the first two, LOWER is
# better for cycle time) -- min-max normalizing each to 0-1 first, and
# inverting cycle time before normalizing it, makes a straight unweighted
# average across the three meaningful. Unweighted on purpose: with no
# stated business priority among the three, picking weights would just
# be inventing a preference the analyst didn't ask for. min_deals filters
# out reps too thin to rank fairly (a rep who closed 1 deal at 100%
# would otherwise look best on win rate alone).

def stage_4_composite_ranking(metrics: dict, min_deals: int = 0) -> list:
    eligible = {k: v for k, v in metrics.items() if v["closed_deals"] >= min_deals}
    win_rate_norm = _normalize({k: v["win_rate_pct"] for k, v in eligible.items()})
    acv_norm = _normalize({k: v["avg_acv"] for k, v in eligible.items()})
    # shorter cycle is better -> invert before normalizing
    cycle_vals = {k: v["avg_cycle_days"] for k, v in eligible.items()}
    inverted_cycle = {k: (-v if v is not None else None) for k, v in cycle_vals.items()}
    cycle_norm = _normalize(inverted_cycle)

    scored = []
    for k in eligible:
        composite = (win_rate_norm.get(k, 0.5) + acv_norm.get(k, 0.5) + cycle_norm.get(k, 0.5)) / 3
        scored.append({"key": k, **eligible[k], "composite_score": round(composite, 3)})
    scored.sort(key=lambda s: s["composite_score"], reverse=True)
    return scored


def run_source_rep_quality_ranking():
    conn = connect()
    evidence = {}

    source_metrics = _quality_metrics(conn, "source")
    print("Stage 1-3 -- Metrics by source:")
    for k, v in source_metrics.items():
        print(f"  {k}: {v}")

    rep_metrics = _quality_metrics(conn, "rep")
    print(f"\nMetrics by rep: {len(rep_metrics)} reps (showing top 5 by closed deal count)")
    for k, v in sorted(rep_metrics.items(), key=lambda kv: kv[1]["closed_deals"], reverse=True)[:5]:
        print(f"  {k}: {v}")

    source_ranking = stage_4_composite_ranking(source_metrics)
    evidence["source_ranking"] = source_ranking
    print("\nStage 4 -- Composite ranking by source:")
    for s in source_ranking:
        print(f"  {s['key']}: score={s['composite_score']} (win_rate={s['win_rate_pct']}%, acv=${s['avg_acv']}, cycle={s['avg_cycle_days']}d)")

    rep_ranking = stage_4_composite_ranking(rep_metrics, min_deals=MIN_CLOSED_DEALS_FOR_REP_RANKING)
    evidence["rep_ranking_top5"] = rep_ranking[:5]
    print(f"\nStage 4 -- Composite ranking by rep (min {MIN_CLOSED_DEALS_FOR_REP_RANKING} closed deals), top 5:")
    for s in rep_ranking[:5]:
        print(f"  {s['key']}: score={s['composite_score']} (win_rate={s['win_rate_pct']}%, acv=${s['avg_acv']}, cycle={s['avg_cycle_days']}d)")

    s5 = claude_synthesize(
        evidence,
        "You are given win rate / ACV / cycle-time rankings by source and by rep. "
        "Summarize which source and which rep produce the best-quality pipeline, "
        "noting any tradeoffs (e.g. one wins on ACV but loses on cycle time), in 2-3 sentences.",
    )
    evidence["stage_5"] = s5
    print("\nStage 5 -- Optional narrative summary:")
    print(f"  {s5['answer']}" if s5["ran"] else f"  (skipped: {s5['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_source_rep_quality_ranking()
