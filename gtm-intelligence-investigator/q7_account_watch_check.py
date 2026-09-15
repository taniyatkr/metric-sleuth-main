"""
Question 7: Is account 199 showing warning signs?

Single-account deep dive, deterministic -- no Claude stage, the answer
is already a clean composite verdict.

  1. Usage/revenue trend vs. its own baseline
  2. Consecutive-decline check
  3. Open/recent tickets
  4. Composite verdict (count of triggered flags -> low/medium/high)

Run directly: python3 q7_account_watch_check.py [account_id]
"""

import sys

from playbook_lib import connect, account_revenue_series

RECENT_MONTHS_WINDOW = 6
BASELINE_MONTHS_WINDOW = 6
DECLINE_VS_BASELINE_THRESHOLD_PCT = 20.0
CONSECUTIVE_DECLINE_MONTHS = 3


def _shift_month(month: str, months_back: int) -> str:
    y, mo = int(month[:4]), int(month[5:7])
    total = (y * 12 + (mo - 1)) - months_back
    return f"{total // 12}-{(total % 12) + 1:02d}"


# ---------------------------------------------------------------------
# Stage 1: trend vs. baseline
# ---------------------------------------------------------------------
# RULE: compare this account's average monthly revenue over the last
# RECENT_MONTHS_WINDOW months to its own average over the
# BASELINE_MONTHS_WINDOW months before that -- an account's own history
# is the baseline, not a segment average, since a normally-small account
# shouldn't be flagged just for being small. Flags if recent is down
# DECLINE_VS_BASELINE_THRESHOLD_PCT or more from baseline.

def stage_1_trend_vs_baseline(conn, account_id: int, as_of_month: str) -> dict:
    baseline_start = _shift_month(as_of_month, RECENT_MONTHS_WINDOW + BASELINE_MONTHS_WINDOW)
    baseline_end = _shift_month(as_of_month, RECENT_MONTHS_WINDOW + 1)
    recent_start = _shift_month(as_of_month, RECENT_MONTHS_WINDOW - 1)

    baseline_series = account_revenue_series(conn, account_id, baseline_start, baseline_end)
    recent_series = account_revenue_series(conn, account_id, recent_start, as_of_month)

    baseline_avg = sum(r for _, r in baseline_series) / len(baseline_series) if baseline_series else None
    recent_avg = sum(r for _, r in recent_series) / len(recent_series) if recent_series else None

    change_pct = None
    below_baseline = False
    if baseline_avg and recent_avg is not None:
        change_pct = (recent_avg - baseline_avg) / baseline_avg * 100
        below_baseline = change_pct <= -DECLINE_VS_BASELINE_THRESHOLD_PCT

    return {
        "stage": "1_trend_vs_baseline",
        "baseline_avg_monthly_revenue": round(baseline_avg, 2) if baseline_avg else None,
        "recent_avg_monthly_revenue": round(recent_avg, 2) if recent_avg is not None else None,
        "change_pct": round(change_pct, 1) if change_pct is not None else None,
        "flag_below_baseline": below_baseline,
    }


# ---------------------------------------------------------------------
# Stage 2: consecutive decline
# ---------------------------------------------------------------------
# RULE: has revenue fallen every single month for the last
# CONSECUTIVE_DECLINE_MONTHS months? A steady slide is a stronger signal
# than a single bad month, which stage 1's average-vs-average comparison
# alone wouldn't distinguish from one noisy dip.

def stage_2_consecutive_decline(conn, account_id: int, as_of_month: str) -> dict:
    start = _shift_month(as_of_month, CONSECUTIVE_DECLINE_MONTHS)
    series = account_revenue_series(conn, account_id, start, as_of_month)
    revs = [r for _, r in series]
    is_consecutively_declining = len(revs) >= CONSECUTIVE_DECLINE_MONTHS and all(
        revs[i] > revs[i + 1] for i in range(len(revs) - 1)
    )
    return {
        "stage": "2_consecutive_decline",
        "recent_monthly_revenue": [round(r, 2) for r in revs],
        "flag_consecutive_decline": is_consecutively_declining,
    }


# ---------------------------------------------------------------------
# Stage 3: recent tickets
# ---------------------------------------------------------------------
# RULE: pulls every ticket in the trailing RECENT_MONTHS_WINDOW for
# context, but only an UNRESOLVED billing_dispute or
# cancellation_request actually trips the flag -- a resolved dispute or
# an unrelated ticket category (e.g. a rate_limit question) isn't a
# churn warning sign on its own.

def stage_3_recent_tickets(conn, account_id: int, as_of_month: str) -> dict:
    start = f"{_shift_month(as_of_month, RECENT_MONTHS_WINDOW)}-01"
    rows = conn.execute("""
        SELECT ticket_id, opened_date, category, priority, resolved FROM support_tickets
        WHERE account_id = ? AND opened_date >= ? ORDER BY opened_date
    """, (account_id, start)).fetchall()
    tickets = [dict(r) for r in rows]
    unresolved_dispute_or_cancel = any(
        t["category"] in ("billing_dispute", "cancellation_request") and not t["resolved"] for t in tickets
    )
    return {
        "stage": "3_recent_tickets",
        "recent_tickets": tickets,
        "flag_unresolved_dispute_or_cancel": unresolved_dispute_or_cancel,
    }


def run_account_watch_check(account_id: int = 199, as_of_month: str = "2026-09"):
    conn = connect()
    evidence = {}

    s1 = stage_1_trend_vs_baseline(conn, account_id, as_of_month)
    evidence["stage_1"] = s1
    print(f"Account {account_id} -- Stage 1: trend vs. baseline")
    print(f"  baseline avg: ${s1['baseline_avg_monthly_revenue']}, recent avg: ${s1['recent_avg_monthly_revenue']}, "
          f"change: {s1['change_pct']}%, flag: {s1['flag_below_baseline']}")

    s2 = stage_2_consecutive_decline(conn, account_id, as_of_month)
    evidence["stage_2"] = s2
    print(f"\nStage 2: consecutive decline")
    print(f"  recent monthly revenue: {s2['recent_monthly_revenue']}, flag: {s2['flag_consecutive_decline']}")

    s3 = stage_3_recent_tickets(conn, account_id, as_of_month)
    evidence["stage_3"] = s3
    print(f"\nStage 3: recent tickets ({len(s3['recent_tickets'])} in the last {RECENT_MONTHS_WINDOW} months)")
    for t in s3["recent_tickets"]:
        print(f"  {t['opened_date']} {t['category']} ({t['priority']}) resolved={bool(t['resolved'])}")
    print(f"  flag: {s3['flag_unresolved_dispute_or_cancel']}")

    # Composite verdict: RULE -- 2 or 3 flags = high, 1 = medium, 0 = low.
    # Deliberately just a flag count, not a weighted score: with only 3
    # binary checks, weighting would be false precision, and a plain
    # count is easy for a reader to audit against the 3 lines above it.
    flags = [s1["flag_below_baseline"], s2["flag_consecutive_decline"], s3["flag_unresolved_dispute_or_cancel"]]
    n_flags = sum(flags)
    verdict = "high" if n_flags >= 2 else "medium" if n_flags == 1 else "low"
    evidence["stage_4_verdict"] = {"n_flags": n_flags, "verdict": verdict}
    print(f"\nStage 4 -- Composite verdict: {n_flags}/3 flags triggered -> risk level: {verdict}")

    conn.close()
    return evidence


if __name__ == "__main__":
    account_id = int(sys.argv[1]) if len(sys.argv) > 1 else 199
    run_account_watch_check(account_id)
