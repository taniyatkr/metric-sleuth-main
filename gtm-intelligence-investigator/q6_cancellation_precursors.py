"""
Question 6: What typically precedes a cancellation for our customers?

This is pattern-mining across every past churn, not one incident's
narrative -- no gates, every stage adds evidence about the whole cohort.

  1. Identify every churned account and its churn month (an account's
     monthly_usage rows simply stop -- there's no churn flag anywhere)
  2. % of churned accounts with a >=20% MoM revenue drop in the window
     before churn (reuses question 1's corrected, revenue-based check)
  3. % of churned accounts with a billing_dispute/cancellation_request
     ticket in the window before churn
  4. Control group: the SAME two checks against active (non-churned)
     accounts in a comparable window -- without this you can't tell if
     these "precursors" are actually predictive or just common anyway
  5. Root-cause synthesis (Claude, bounded)

Run directly: python3 q6_cancellation_precursors.py
"""

import random

from playbook_lib import connect, account_had_usage_drop, claude_synthesize

PRECHURN_WINDOW_MONTHS = 3
CHURN_LAG_BUFFER_MONTHS = 2  # an account must be silent for at least this
                              # many months before the dataset's max month
                              # to count as churned, not just recently quiet


def _shift_month(month: str, months_back: int) -> str:
    y, mo = int(month[:4]), int(month[5:7])
    total = (y * 12 + (mo - 1)) - months_back
    return f"{total // 12}-{(total % 12) + 1:02d}"


def stage_1_identify_churned_accounts(conn) -> dict:
    global_max = conn.execute("SELECT MAX(month) FROM monthly_usage").fetchone()[0]
    cutoff = _shift_month(global_max, CHURN_LAG_BUFFER_MONTHS)

    rows = conn.execute("""
        SELECT account_id, MAX(month) AS last_month FROM monthly_usage
        GROUP BY account_id HAVING last_month < ?
    """, (cutoff,)).fetchall()

    churned = {r["account_id"]: _shift_month(r["last_month"], -1) for r in rows}  # churn_month = month after last activity
    active = [r["account_id"] for r in conn.execute("SELECT DISTINCT account_id FROM monthly_usage") if r["account_id"] not in churned]

    return {
        "stage": "1_identify_churned",
        "global_max_month": global_max,
        "n_churned": len(churned),
        "n_active": len(active),
        "churned_accounts": churned,   # {account_id: churn_month}
        "active_accounts": active,
        "stop_here": False,
    }


def _has_recent_usage_drop(conn, account_id: int, reference_month: str) -> bool:
    start = _shift_month(reference_month, PRECHURN_WINDOW_MONTHS)
    end = _shift_month(reference_month, 1)  # window ends just before reference_month
    return account_had_usage_drop(conn, account_id, start, end)


def _has_recent_dispute_ticket(conn, account_id: int, reference_month: str) -> bool:
    start = f"{_shift_month(reference_month, PRECHURN_WINDOW_MONTHS)}-01"
    end = f"{reference_month}-01"
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM support_tickets
        WHERE account_id = ? AND category IN ('billing_dispute', 'cancellation_request')
          AND opened_date >= ? AND opened_date < ?
    """, (account_id, start, end)).fetchone()
    return row["n"] > 0


def stage_2_and_3_precursor_rates(conn, accounts_with_reference_months: dict) -> dict:
    n = len(accounts_with_reference_months)
    if n == 0:
        return {"n": 0, "usage_drop_pct": None, "dispute_ticket_pct": None}
    n_usage_drop = sum(_has_recent_usage_drop(conn, aid, ref) for aid, ref in accounts_with_reference_months.items())
    n_dispute = sum(_has_recent_dispute_ticket(conn, aid, ref) for aid, ref in accounts_with_reference_months.items())
    return {
        "n": n,
        "usage_drop_pct": round(n_usage_drop / n * 100, 1),
        "dispute_ticket_pct": round(n_dispute / n * 100, 1),
    }


def stage_4_control_group(conn, active_accounts: list, reference_month: str, sample_size: int = 100) -> dict:
    random.seed(42)  # reproducible sample
    sample = random.sample(active_accounts, min(sample_size, len(active_accounts)))
    reference_months = {aid: reference_month for aid in sample}
    return stage_2_and_3_precursor_rates(conn, reference_months)


def run_cancellation_precursor_analysis():
    conn = connect()
    evidence = {}

    s1 = stage_1_identify_churned_accounts(conn)
    evidence["stage_1"] = {k: v for k, v in s1.items() if k not in ("churned_accounts", "active_accounts")}
    print(f"Stage 1 -- Identify churned accounts:")
    print(f"  {s1['n_churned']} churned, {s1['n_active']} still active (as of {s1['global_max_month']})")

    churned_refs = s1["churned_accounts"]
    s23 = stage_2_and_3_precursor_rates(conn, churned_refs)
    evidence["stage_2_3_churned"] = s23
    print(f"\nStage 2-3 -- Precursor rates among churned accounts (n={s23['n']}):")
    print(f"  usage_drop_pct: {s23['usage_drop_pct']}%, dispute_ticket_pct: {s23['dispute_ticket_pct']}%")

    s4 = stage_4_control_group(conn, s1["active_accounts"], s1["global_max_month"])
    evidence["stage_4_control"] = s4
    print(f"\nStage 4 -- Same rates among a control sample of active accounts (n={s4['n']}):")
    print(f"  usage_drop_pct: {s4['usage_drop_pct']}%, dispute_ticket_pct: {s4['dispute_ticket_pct']}%")

    usage_lift = s23["usage_drop_pct"] - s4["usage_drop_pct"]
    dispute_lift = s23["dispute_ticket_pct"] - s4["dispute_ticket_pct"]
    evidence["lift"] = {"usage_drop_lift_pts": round(usage_lift, 1), "dispute_ticket_lift_pts": round(dispute_lift, 1)}
    print(f"\n  Lift vs. control: usage_drop +{usage_lift:.1f} pts, dispute_ticket +{dispute_lift:.1f} pts")

    s5 = claude_synthesize(
        evidence,
        "You are given churned-vs-control precursor rates a fixed analytics pipeline "
        "collected. Describe the typical pattern that precedes a cancellation, and note "
        "whether each precursor is actually predictive (meaningful lift over the control "
        "group) or just common among active accounts generally, in 2-3 sentences.",
    )
    evidence["stage_5"] = s5
    print("\nStage 5 -- Synthesis:")
    print(f"  {s5['answer']}" if s5["ran"] else f"  (skipped: {s5['reason']})")

    conn.close()
    return evidence


if __name__ == "__main__":
    run_cancellation_precursor_analysis()
