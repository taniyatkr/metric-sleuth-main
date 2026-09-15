"""
Question 4: What's driving our overall NRR this quarter (ending Aug 2026)?

Locked stage order:
  1. Decompose company-wide NRR by segment       -- diagnostic, finds the
     worst-performing segment
  2. Trend check across trailing quarters         -- diagnostic
  3. ARR waterfall decomposition for the quarter's ending month
                                                    -- diagnostic
  4. Account-level concentration within the worst segment (reuses
     question 1's concentration_and_pace rule)     -- diagnostic
  5. Root-cause synthesis -- if the worst segment is Mid-Market, this
     hands off to question 1's full flagship playbook instead of
     re-deriving the same investigation; otherwise a direct Claude
     synthesis over stages 1-4.

Run directly: python3 q4_overall_nrr.py
"""

from playbook_lib import connect, retention_by_segment, arr_waterfall_buckets, concentration_and_pace, claude_synthesize


def stage_1_segment_decomposition(conn, base_month: str, current_month: str) -> dict:
    by_segment = retention_by_segment(conn, base_month, current_month)
    blended = by_segment.pop("blended")
    worst_segment = min(
        (seg for seg in by_segment if by_segment[seg]),
        key=lambda seg: by_segment[seg]["nrr_pct"],
    )
    return {
        "stage": "1_segment_decomposition",
        "blended": blended,
        "by_segment": by_segment,
        "worst_segment": worst_segment,
        "stop_here": False,
    }


def stage_2_trend_check(conn, current_month: str) -> dict:
    def shift_month(m, months_back):
        y, mo = int(m[:4]), int(m[5:7])
        total = (y * 12 + (mo - 1)) - months_back
        return f"{total // 12}-{(total % 12) + 1:02d}"

    windows = {}
    for q_back in (0, 1, 2, 3):
        cur = shift_month(current_month, q_back * 3)
        base = shift_month(cur, 12)
        blended = retention_by_segment(conn, base, cur).get("blended")
        if blended:
            windows[f"{base}->{cur}"] = blended["nrr_pct"]

    # windows is ordered most-recent-quarter first (q_back=0); reverse to
    # check whether NRR has been declining quarter over quarter
    trend_order = list(reversed(windows.values()))
    is_ongoing_decline = len(trend_order) >= 3 and all(
        trend_order[i] >= trend_order[i + 1] for i in range(len(trend_order) - 1)
    )

    return {
        "stage": "2_trend_check",
        "trailing_quarterly_nrr": windows,
        "is_ongoing_decline": is_ongoing_decline,
        "stop_here": False,
    }


def stage_3_waterfall(conn, month: str) -> dict:
    buckets = arr_waterfall_buckets(conn, month)
    buckets["stage"] = "3_waterfall"
    buckets["stop_here"] = False
    return buckets


def stage_4_account_concentration(conn, worst_segment: str, base_month: str, current_month: str) -> dict:
    result = concentration_and_pace(conn, worst_segment, base_month, current_month)
    result["stage"] = "4_account_concentration"
    result["segment"] = worst_segment
    return result


def run_overall_nrr_playbook(base_month="2025-08", current_month="2026-08"):
    conn = connect()
    evidence = {}

    s1 = stage_1_segment_decomposition(conn, base_month, current_month)
    evidence["stage_1"] = s1
    print("Stage 1 -- Segment decomposition:")
    print(f"  blended: {s1['blended']}")
    for seg, s in s1["by_segment"].items():
        print(f"  {seg}: {s}")
    print(f"  worst segment: {s1['worst_segment']}")

    s2 = stage_2_trend_check(conn, current_month)
    evidence["stage_2"] = s2
    print("\nStage 2 -- Trend check (trailing quarters, blended NRR):")
    print(f"  {s2['trailing_quarterly_nrr']}")
    print(f"  is_ongoing_decline: {s2['is_ongoing_decline']}")

    s3 = stage_3_waterfall(conn, current_month)
    evidence["stage_3"] = s3
    print("\nStage 3 -- ARR waterfall for the quarter's ending month:")
    print(f"  new=${s3['new']:,.2f} expansion=${s3['expansion']:,.2f} "
          f"contraction=${s3['contraction']:,.2f} churned=${s3['churned']:,.2f} net_new=${s3['net_new']:,.2f}")

    s4 = stage_4_account_concentration(conn, s1["worst_segment"], base_month, current_month)
    evidence["stage_4"] = s4
    print(f"\nStage 4 -- Account concentration within {s1['worst_segment']}:")
    print(f"  {s4}")

    if s1["worst_segment"] == "Mid-Market":
        print("\nStage 5 -- Worst segment is Mid-Market: handing off to question 1's flagship playbook "
              "instead of re-deriving the same investigation.")
        from retention_drop_playbook import run_flagship_playbook
        s5_evidence = run_flagship_playbook(segment="Mid-Market", drop_month=current_month,
                                             base_month=base_month, current_month=current_month)
        evidence["stage_5"] = {"stage": "5_handoff_to_question_1", "delegated_evidence": s5_evidence}
    else:
        s5 = claude_synthesize(
            evidence,
            f"You are given evidence a fixed analytics pipeline collected while investigating "
            f"what's driving overall NRR this quarter. The worst-performing segment is "
            f"{s1['worst_segment']}. State the most likely root cause in 2-3 sentences.",
        )
        evidence["stage_5"] = s5
        print("\nStage 5 -- Root-cause synthesis:")
        print(f"  {s5['answer']}" if s5["ran"] else f"  (skipped: {s5['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_overall_nrr_playbook()
