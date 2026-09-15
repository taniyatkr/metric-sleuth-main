"""
Question 3: Why are we seeing a spike in billing complaints this spring?

Locked stage order:
  1. Confirm the spike is real (vs. prior years, same months)   -- gate
  2. Segment concentration                                       -- diagnostic
  3. Channel concentration (within the flagged segment)          -- diagnostic
  4. Timing shape (single-month spike vs. gradual rise)          -- diagnostic
  5. Resolution-rate check (support capacity signal)             -- diagnostic
  6. Rate-stability check (per-channel units-vs-revenue, shared with
     question 1 -- channel_rates is static in this dataset, so this
     confirms whether price is a factor rather than assuming it isn't)
                                                                    -- diagnostic
  7. Root-cause synthesis (Claude, bounded)

Run directly: python3 q3_billing_complaint_spike.py
"""

from playbook_lib import connect, channel_level_trend, claude_synthesize

SPRING_MONTHS = ("03", "04", "05")
SPIKE_MULTIPLE_THRESHOLD = 2.0
MIN_ABSOLUTE_SPIKE_COUNT = 10
SEGMENT_CONCENTRATION_THRESHOLD = 0.60
CHANNEL_CONCENTRATION_THRESHOLD = 0.60
SINGLE_MONTH_SHARE_THRESHOLD = 0.50
RESOLUTION_DROP_PTS_THRESHOLD = 20.0


# ---------------------------------------------------------------------
# Stage 1: Confirm the spike is real
# ---------------------------------------------------------------------

def _billing_dispute_count(conn, year: int, months=SPRING_MONTHS):
    placeholders = ",".join("?" for _ in months)
    row = conn.execute(f"""
        SELECT COUNT(*) AS n FROM support_tickets
        WHERE category = 'billing_dispute'
          AND strftime('%Y', opened_date) = ?
          AND strftime('%m', opened_date) IN ({placeholders})
    """, (str(year), *months)).fetchone()
    return row["n"]


def stage_1_confirm_spike(conn, year: int, months=SPRING_MONTHS) -> dict:
    this_year_count = _billing_dispute_count(conn, year, months)
    prior_counts = {}
    for years_back in (1, 2):
        c = _billing_dispute_count(conn, year - years_back, months)
        prior_counts[year - years_back] = c

    prior_values = list(prior_counts.values())
    avg_prior = sum(prior_values) / len(prior_values) if prior_values else 0

    if avg_prior > 0:
        multiple = this_year_count / avg_prior
        is_spike = multiple >= SPIKE_MULTIPLE_THRESHOLD
    else:
        multiple = None
        is_spike = this_year_count >= MIN_ABSOLUTE_SPIKE_COUNT

    return {
        "stage": "1_confirm_spike",
        "this_year_count": this_year_count,
        "prior_year_counts": prior_counts,
        "multiple_vs_prior_avg": round(multiple, 2) if multiple is not None else None,
        "is_spike": is_spike,
        "stop_here": not is_spike,
    }


# ---------------------------------------------------------------------
# Stage 2: Segment concentration
# ---------------------------------------------------------------------

def stage_2_segment_concentration(conn, year: int, months=SPRING_MONTHS) -> dict:
    placeholders = ",".join("?" for _ in months)
    rows = conn.execute(f"""
        SELECT a.segment AS segment, COUNT(*) AS n
        FROM support_tickets t JOIN accounts a ON a.account_id = t.account_id
        WHERE t.category = 'billing_dispute'
          AND strftime('%Y', t.opened_date) = ?
          AND strftime('%m', t.opened_date) IN ({placeholders})
        GROUP BY a.segment ORDER BY n DESC
    """, (str(year), *months)).fetchall()

    total = sum(r["n"] for r in rows)
    breakdown = {r["segment"]: r["n"] for r in rows}
    top_segment, top_n = (rows[0]["segment"], rows[0]["n"]) if rows else (None, 0)
    top_share = top_n / total if total else 0.0

    return {
        "stage": "2_segment_concentration",
        "breakdown": breakdown,
        "top_segment": top_segment,
        "top_segment_share": round(top_share, 3),
        "segment_concentrated": top_share >= SEGMENT_CONCENTRATION_THRESHOLD,
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 3: Channel concentration (within the flagged segment)
# ---------------------------------------------------------------------

def stage_3_channel_concentration(conn, segment: str, year: int, months=SPRING_MONTHS) -> dict:
    placeholders = ",".join("?" for _ in months)
    rows = conn.execute(f"""
        SELECT a.primary_channel AS channel, COUNT(*) AS n
        FROM support_tickets t JOIN accounts a ON a.account_id = t.account_id
        WHERE t.category = 'billing_dispute' AND a.segment = ?
          AND strftime('%Y', t.opened_date) = ?
          AND strftime('%m', t.opened_date) IN ({placeholders})
        GROUP BY a.primary_channel ORDER BY n DESC
    """, (segment, str(year), *months)).fetchall()

    total = sum(r["n"] for r in rows)
    breakdown = {r["channel"]: r["n"] for r in rows}
    top_channel, top_n = (rows[0]["channel"], rows[0]["n"]) if rows else (None, 0)
    top_share = top_n / total if total else 0.0

    return {
        "stage": "3_channel_concentration",
        "segment": segment,
        "breakdown": breakdown,
        "top_channel": top_channel,
        "top_channel_share": round(top_share, 3),
        "channel_concentrated": top_share >= CHANNEL_CONCENTRATION_THRESHOLD,
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 4: Timing shape
# ---------------------------------------------------------------------

def stage_4_timing_shape(conn, year: int, months=SPRING_MONTHS) -> dict:
    by_month = {}
    for m in months:
        row = conn.execute("""
            SELECT COUNT(*) AS n FROM support_tickets
            WHERE category = 'billing_dispute'
              AND strftime('%Y', opened_date) = ? AND strftime('%m', opened_date) = ?
        """, (str(year), m)).fetchone()
        by_month[f"{year}-{m}"] = row["n"]

    total = sum(by_month.values())
    peak_month, peak_n = max(by_month.items(), key=lambda kv: kv[1]) if total else (None, 0)
    peak_share = peak_n / total if total else 0.0

    return {
        "stage": "4_timing_shape",
        "by_month": by_month,
        "peak_month": peak_month,
        "peak_month_share": round(peak_share, 3),
        "shape": "single_month_spike" if peak_share >= SINGLE_MONTH_SHARE_THRESHOLD else "gradual_rise",
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 5: Resolution-rate check
# ---------------------------------------------------------------------

def _resolution_rate(conn, year: int, months=SPRING_MONTHS) -> float | None:
    placeholders = ",".join("?" for _ in months)
    row = conn.execute(f"""
        SELECT COUNT(*) AS n, SUM(resolved) AS n_resolved FROM support_tickets
        WHERE category = 'billing_dispute'
          AND strftime('%Y', opened_date) = ?
          AND strftime('%m', opened_date) IN ({placeholders})
    """, (str(year), *months)).fetchone()
    if not row["n"]:
        return None
    return row["n_resolved"] / row["n"] * 100


def stage_5_resolution_rate(conn, year: int, months=SPRING_MONTHS) -> dict:
    this_year_rate = _resolution_rate(conn, year, months)
    prior_year_rate = _resolution_rate(conn, year - 1, months)

    drop_pts = None
    capacity_signal = False
    if this_year_rate is not None and prior_year_rate is not None:
        drop_pts = prior_year_rate - this_year_rate
        capacity_signal = drop_pts >= RESOLUTION_DROP_PTS_THRESHOLD

    return {
        "stage": "5_resolution_rate",
        "this_year_resolution_rate_pct": round(this_year_rate, 1) if this_year_rate is not None else None,
        "prior_year_resolution_rate_pct": round(prior_year_rate, 1) if prior_year_rate is not None else None,
        "drop_pts": round(drop_pts, 1) if drop_pts is not None else None,
        "resolution_capacity_signal": capacity_signal,
        "stop_here": False,
    }


# ---------------------------------------------------------------------
# Stage 6: Rate-stability check (shared with question 1)
# ---------------------------------------------------------------------
# Uses playbook_lib.channel_level_trend directly rather than a local
# copy -- both questions need the exact same per-channel comparison, and
# duplicating it risks reintroducing the summed-across-channels bug that
# question 1 originally shipped with (see METHODOLOGY.md for the story).

def stage_6_rate_stability(conn, segment: str, pre_month: str, spike_month: str) -> dict:
    result = channel_level_trend(conn, segment, pre_month, spike_month)
    result["stage"] = "6_rate_stability"
    result["stop_here"] = False
    return result


def run_billing_spike_playbook(year=2026, months=SPRING_MONTHS, pre_month="2026-02", spike_month="2026-03"):
    conn = connect()
    evidence = {}

    s1 = stage_1_confirm_spike(conn, year, months)
    evidence["stage_1"] = s1
    print("Stage 1 -- Confirm spike is real:")
    print(f"  {year} billing_dispute count: {s1['this_year_count']}, prior years: {s1['prior_year_counts']}")
    print(f"  multiple vs prior avg: {s1['multiple_vs_prior_avg']}, is_spike: {s1['is_spike']}")
    if s1["stop_here"]:
        print("\n  -> STOP: not actually elevated beyond normal variance.")
        conn.close()
        return evidence

    s2 = stage_2_segment_concentration(conn, year, months)
    evidence["stage_2"] = s2
    print("\nStage 2 -- Segment concentration:")
    print(f"  breakdown: {s2['breakdown']}")
    print(f"  top segment: {s2['top_segment']} ({s2['top_segment_share']:.1%})")

    s3 = stage_3_channel_concentration(conn, s2["top_segment"], year, months)
    evidence["stage_3"] = s3
    print("\nStage 3 -- Channel concentration (within top segment):")
    print(f"  breakdown: {s3['breakdown']}")
    print(f"  top channel: {s3['top_channel']} ({s3['top_channel_share']:.1%})")

    s4 = stage_4_timing_shape(conn, year, months)
    evidence["stage_4"] = s4
    print("\nStage 4 -- Timing shape:")
    print(f"  by month: {s4['by_month']}, shape: {s4['shape']}")

    s5 = stage_5_resolution_rate(conn, year, months)
    evidence["stage_5"] = s5
    print("\nStage 5 -- Resolution-rate check:")
    print(f"  this year: {s5['this_year_resolution_rate_pct']}%, prior year: {s5['prior_year_resolution_rate_pct']}%, "
          f"drop: {s5['drop_pts']} pts, capacity_signal: {s5['resolution_capacity_signal']}")

    s6 = stage_6_rate_stability(conn, s2["top_segment"], pre_month, spike_month)
    evidence["stage_6"] = s6
    print("\nStage 6 -- Rate-stability check (per channel):")
    for ch, c in s6["per_channel"].items():
        print(f"  {ch}: units {c['units_change_pct']:+.1f}%, revenue {c['revenue_change_pct']:+.1f}%, gap {c['gap_pts']:+.1f} pts")
    print(f"  rate_effect_detected: {s6['rate_effect_detected']}, dominant_declining_channel: {s6['dominant_declining_channel']}")

    s7 = claude_synthesize(
        evidence,
        "You are given evidence a fixed analytics pipeline collected while investigating "
        "a spring billing-complaint spike. stage_6.rate_effect_detected confirms whether "
        "price is a factor -- if False, do not describe this as a price change; describe "
        "it as a usage/ticket-driven event instead. State the most likely root cause in "
        "2-3 sentences.",
    )
    evidence["stage_7"] = s7
    print("\nStage 7 -- Root-cause synthesis:")
    print(f"  {s7['answer']}" if s7["ran"] else f"  (skipped: {s7['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_billing_spike_playbook()
