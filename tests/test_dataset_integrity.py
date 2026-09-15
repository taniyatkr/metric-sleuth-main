"""
Data-quality tests for data/metric_sleuth.db itself -- independent of the
MCP server. These are the tests that would catch a broken regeneration of
the dataset (a bad edit to the generator scripts, a bug that orphans
rows, a benchmark that silently drifted out of its target range) before
it ever reached the MCP tools built on top of it.
"""

import sqlite3

VALID_SEGMENTS = {"SMB", "Mid-Market", "Enterprise"}
VALID_STAGES = {
    "Prospecting", "Qualification", "Demo", "Proposal", "Negotiation",
    "Closed Won", "Closed Lost",
}
VALID_CHANNELS = {"sms", "voice", "email", "verification"}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}


def test_all_five_tables_exist(db_conn):
    tables = {r["name"] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"opportunities", "accounts", "channel_rates",
            "monthly_usage", "support_tickets"} <= tables


def test_row_counts_are_plausible(db_conn):
    counts = {
        t: db_conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("opportunities", "accounts", "channel_rates",
                   "monthly_usage", "support_tickets")
    }
    assert counts["opportunities"] > 500
    assert counts["channel_rates"] == 4
    assert 50 < counts["accounts"] < counts["opportunities"]
    assert counts["monthly_usage"] > counts["accounts"] * 5
    assert counts["support_tickets"] > 0


def test_accounts_only_come_from_won_opportunities(db_conn):
    bad = db_conn.execute("""
        SELECT COUNT(*) c FROM accounts a
        JOIN opportunities o ON o.opportunity_id = a.opportunity_id
        WHERE o.is_won != 1
    """).fetchone()["c"]
    assert bad == 0


def test_no_orphaned_usage_rows(db_conn):
    bad = db_conn.execute("""
        SELECT COUNT(*) c FROM monthly_usage u
        LEFT JOIN accounts a ON a.account_id = u.account_id
        WHERE a.account_id IS NULL
    """).fetchone()["c"]
    assert bad == 0


def test_no_orphaned_ticket_rows(db_conn):
    bad = db_conn.execute("""
        SELECT COUNT(*) c FROM support_tickets t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        WHERE a.account_id IS NULL
    """).fetchone()["c"]
    assert bad == 0


def test_segments_are_valid_everywhere(db_conn):
    for table in ("opportunities", "accounts"):
        segs = {r["segment"] for r in db_conn.execute(f"SELECT DISTINCT segment FROM {table}")}
        assert segs <= VALID_SEGMENTS, f"unexpected segment(s) in {table}: {segs - VALID_SEGMENTS}"


def test_opportunity_stages_are_valid(db_conn):
    stages = {r["stage"] for r in db_conn.execute("SELECT DISTINCT stage FROM opportunities")}
    assert stages <= VALID_STAGES, f"unexpected stage(s): {stages - VALID_STAGES}"


def test_channels_are_valid(db_conn):
    acct_channels = {r["primary_channel"] for r in db_conn.execute(
        "SELECT DISTINCT primary_channel FROM accounts"
    )}
    usage_channels = {r["channel"] for r in db_conn.execute(
        "SELECT DISTINCT channel FROM monthly_usage"
    )}
    assert acct_channels <= VALID_CHANNELS
    assert usage_channels <= VALID_CHANNELS


def test_ticket_priorities_are_valid(db_conn):
    priorities = {r["priority"] for r in db_conn.execute(
        "SELECT DISTINCT priority FROM support_tickets"
    )}
    assert priorities <= VALID_PRIORITIES


def test_won_opportunities_have_closed_amount_lost_dont(db_conn):
    bad_won = db_conn.execute(
        "SELECT COUNT(*) c FROM opportunities WHERE is_won=1 AND closed_amount IS NULL"
    ).fetchone()["c"]
    bad_lost = db_conn.execute(
        "SELECT COUNT(*) c FROM opportunities WHERE is_won=0 AND closed_amount IS NOT NULL"
    ).fetchone()["c"]
    assert bad_won == 0
    assert bad_lost == 0


def test_open_opportunities_have_no_close_date(db_conn):
    bad = db_conn.execute(
        "SELECT COUNT(*) c FROM opportunities WHERE is_won IS NULL AND close_date IS NOT NULL"
    ).fetchone()["c"]
    assert bad == 0


def test_revenue_and_units_are_non_negative(db_conn):
    bad = db_conn.execute(
        "SELECT COUNT(*) c FROM monthly_usage WHERE revenue < 0 OR units < 1"
    ).fetchone()["c"]
    assert bad == 0


def test_dates_are_parseable_iso(db_conn):
    import datetime
    cols = [
        ("opportunities", "created_date"), ("opportunities", "close_date"),
        ("accounts", "signup_date"), ("support_tickets", "opened_date"),
    ]
    for table, col in cols:
        for r in db_conn.execute(f"SELECT {col} AS v FROM {table} WHERE {col} IS NOT NULL"):
            datetime.date.fromisoformat(r["v"])  # raises ValueError if malformed


def test_blended_win_rate_is_in_sourced_range(db_conn):
    """Gradient.works' 2025 B2B benchmarks put overall win rate at ~20-21%.
    We allow some slack since this is a stochastic generator, but a wildly
    off result here means the generator's calibration broke."""
    row = db_conn.execute("""
        SELECT SUM(CASE WHEN is_won=1 THEN 1 ELSE 0 END) AS won,
               SUM(CASE WHEN is_won=0 THEN 1 ELSE 0 END) AS lost
        FROM opportunities WHERE is_won IS NOT NULL
    """).fetchone()
    win_rate = row["won"] / (row["won"] + row["lost"])
    assert 0.15 < win_rate < 0.28


def test_pre_incident_retention_lands_in_bessemer_bands(db_conn):
    """Feb 2025 -> Feb 2026 predates the planted March 2026 incident, so
    this is the clean calibration check: every segment's NRR/GRR should
    land inside its Bessemer-sourced target band (see data/METHODOLOGY.md).
    Bands are widened slightly (2pts) to tolerate generator stochasticity."""
    targets = {
        "SMB": {"nrr": (78, 102), "grr": (68, 82)},
        "Mid-Market": {"nrr": (88, 122), "grr": (78, 92)},
        "Enterprise": {"nrr": (98, 999), "grr": (88, 999)},
    }
    base = db_conn.execute("""
        SELECT a.account_id, a.segment, SUM(u.revenue) AS base_rev
        FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
        WHERE u.month = '2025-02' GROUP BY a.account_id, a.segment
    """).fetchall()
    cur = {r["account_id"]: r["cur_rev"] for r in db_conn.execute(
        "SELECT account_id, SUM(revenue) AS cur_rev FROM monthly_usage WHERE month='2026-02' "
        "GROUP BY account_id"
    )}
    by_segment: dict[str, list] = {}
    for r in base:
        by_segment.setdefault(r["segment"], []).append(r)

    for segment, rows in by_segment.items():
        base_total = sum(r["base_rev"] for r in rows)
        cur_total = sum(cur.get(r["account_id"], 0.0) for r in rows)
        capped_total = sum(min(r["base_rev"], cur.get(r["account_id"], 0.0)) for r in rows)
        nrr = cur_total / base_total * 100
        grr = capped_total / base_total * 100
        lo_nrr, hi_nrr = targets[segment]["nrr"]
        lo_grr, hi_grr = targets[segment]["grr"]
        assert lo_nrr <= nrr <= hi_nrr, f"{segment} NRR {nrr:.1f}% outside [{lo_nrr}, {hi_nrr}]"
        assert lo_grr <= grr <= hi_grr, f"{segment} GRR {grr:.1f}% outside [{lo_grr}, {hi_grr}]"


def test_incident_signature_is_present(db_conn):
    """Regression test for the planted March 2026 SMS/Mid-Market price
    hike: this doesn't assert the story is true (METHODOLOGY.md documents
    that), it asserts the signature stays discoverable -- billing_dispute
    tickets concentrated in that exact segment/channel/window, clearly
    above the rest of the segment in the same window. If a future
    regeneration breaks this, the Investigator has nothing to find."""
    incident_tickets = db_conn.execute("""
        SELECT COUNT(*) c FROM support_tickets t
        JOIN accounts a ON a.account_id = t.account_id
        WHERE a.segment='Mid-Market' AND a.primary_channel='sms'
          AND t.category='billing_dispute'
          AND t.opened_date BETWEEN '2026-03-01' AND '2026-04-30'
    """).fetchone()["c"]
    rest_tickets = db_conn.execute("""
        SELECT COUNT(*) c FROM support_tickets t
        JOIN accounts a ON a.account_id = t.account_id
        WHERE NOT (a.segment='Mid-Market' AND a.primary_channel='sms')
          AND t.category='billing_dispute'
          AND t.opened_date BETWEEN '2026-03-01' AND '2026-04-30'
    """).fetchone()["c"]
    assert incident_tickets > 20
    assert incident_tickets > rest_tickets * 3
