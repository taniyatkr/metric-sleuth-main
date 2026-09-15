"""
Question 5: Which accounts are at risk of churning right now?

This is a ranking, not a single root-cause narrative -- no Claude stage.
Every currently-active account gets scored on 3 fixed signals; the
composite score is just how many fired. No stage stops the pipeline
early, since every account needs to be scored, not narrowed down to one
explanation.

  1. Usage-decline signal (>=20% MoM drop in the trailing window)
  2. Billing-dispute / cancellation-request signal (unresolved, recent)
  3. Revenue-trajectory signal (declining several consecutive months,
     approaching the churn floor)
  4. Composite score = count of triggered signals, ranked

Run directly: python3 q5_churn_risk_scoring.py
"""

from playbook_lib import connect, account_had_usage_drop

# Every account is checked over a trailing 3-month window as of
# as_of_month -- long enough to catch a real trend, short enough that
# the score still reflects CURRENT risk rather than ancient history.
RECENT_MONTHS_WINDOW = 3
CHURN_FLOOR_USD = 5.0
NEAR_FLOOR_MULTIPLE = 10.0  # flag accounts within 10x the churn floor --
                            # i.e. revenue has shrunk close enough to
                            # nothing that the next drop could be the
                            # last one
CONSECUTIVE_DECLINE_MONTHS = 3


def _shift_month(month: str, months_back: int) -> str:
    y, mo = int(month[:4]), int(month[5:7])
    total = (y * 12 + (mo - 1)) - months_back
    return f"{total // 12}-{(total % 12) + 1:02d}"


def active_accounts(conn, as_of_month: str) -> list:
    """Accounts with revenue > 0 in as_of_month -- not already churned."""
    rows = conn.execute("""
        SELECT account_id, SUM(revenue) AS rev FROM monthly_usage
        WHERE month = ? GROUP BY account_id HAVING rev > 0
    """, (as_of_month,)).fetchall()
    return [r["account_id"] for r in rows]


# ---------------------------------------------------------------------
# Signal 1: usage decline
# ---------------------------------------------------------------------
# RULE: reuses playbook_lib.account_had_usage_drop -- the same corrected,
# revenue-based (never units-based) >=20% MoM check questions 1 and 6
# also rely on, applied here to the trailing RECENT_MONTHS_WINDOW.

def signal_usage_decline(conn, account_id: int, as_of_month: str) -> bool:
    start = _shift_month(as_of_month, RECENT_MONTHS_WINDOW)
    return account_had_usage_drop(conn, account_id, start, as_of_month)


# ---------------------------------------------------------------------
# Signal 2: billing dispute or cancellation request, still open
# ---------------------------------------------------------------------
# RULE: an UNRESOLVED billing_dispute or cancellation_request ticket
# opened within the trailing window -- a resolved ticket, or one from
# outside the window, doesn't count. This is a direct expressed-intent
# signal, not an inferred one.

def signal_billing_or_cancellation(conn, account_id: int, as_of_month: str) -> bool:
    start = _shift_month(as_of_month, RECENT_MONTHS_WINDOW)
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM support_tickets
        WHERE account_id = ? AND category IN ('billing_dispute', 'cancellation_request')
          AND resolved = 0 AND opened_date >= ?
    """, (account_id, f"{start}-01")).fetchone()
    return row["n"] > 0


# ---------------------------------------------------------------------
# Signal 3: revenue trajectory
# ---------------------------------------------------------------------
# RULE: fires only when BOTH conditions hold -- revenue has fallen every
# single month for CONSECUTIVE_DECLINE_MONTHS in a row (a steady slide,
# not one noisy month), AND the most recent month is already within
# NEAR_FLOOR_MULTIPLE of CHURN_FLOOR_USD (close enough to zero that the
# slide is nearly played out). Either condition alone is common and not
# very predictive on its own; requiring both is what makes this signal
# worth a point in the composite score.

def signal_revenue_trajectory(conn, account_id: int, as_of_month: str) -> bool:
    start = _shift_month(as_of_month, CONSECUTIVE_DECLINE_MONTHS)
    rows = conn.execute("""
        SELECT month, SUM(revenue) AS rev FROM monthly_usage
        WHERE account_id = ? AND month >= ? AND month <= ?
        GROUP BY month ORDER BY month
    """, (account_id, start, as_of_month)).fetchall()
    revs = [r["rev"] for r in rows]
    if len(revs) < CONSECUTIVE_DECLINE_MONTHS:
        return False
    consecutively_declining = all(revs[i] > revs[i + 1] for i in range(len(revs) - 1))
    near_floor = revs[-1] <= CHURN_FLOOR_USD * NEAR_FLOOR_MULTIPLE
    return consecutively_declining and near_floor


# ---------------------------------------------------------------------
# Composite score = how many of the 3 signals fired (0-3), for one account
# ---------------------------------------------------------------------

def score_account(conn, account_id: int, as_of_month: str) -> dict:
    signals = {
        "usage_decline": signal_usage_decline(conn, account_id, as_of_month),
        "billing_or_cancellation": signal_billing_or_cancellation(conn, account_id, as_of_month),
        "revenue_trajectory": signal_revenue_trajectory(conn, account_id, as_of_month),
    }
    return {"account_id": account_id, "signals": signals, "score": sum(signals.values())}


def run_churn_risk_scoring(as_of_month="2026-09", top_n=15):
    conn = connect()
    accounts = active_accounts(conn, as_of_month)
    print(f"Scoring {len(accounts)} active accounts as of {as_of_month}...")

    scored = [score_account(conn, aid, as_of_month) for aid in accounts]
    scored = [s for s in scored if s["score"] > 0]
    scored.sort(key=lambda s: s["score"], reverse=True)

    print(f"\n{len(scored)} accounts triggered at least one risk signal. Top {top_n}:")
    for s in scored[:top_n]:
        fired = [k for k, v in s["signals"].items() if v]
        print(f"  account {s['account_id']}: score={s['score']}/3, signals=({', '.join(fired)})")

    conn.close()
    return scored


if __name__ == "__main__":
    run_churn_risk_scoring()
