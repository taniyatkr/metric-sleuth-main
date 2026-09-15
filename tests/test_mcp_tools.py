"""
Tests for mcp_server/analytics_server.py's tool functions, called directly
(not through the MCP protocol -- see test_mcp_protocol.py for that). These
are fast and check both correctness and the safety guardrails on
run_query.
"""

import pytest

import mcp_server.analytics_server as srv


def test_get_schema_lists_all_tables():
    schema = srv.get_schema()
    for table in ("opportunities", "accounts", "channel_rates",
                   "monthly_usage", "support_tickets"):
        assert f"{table}:" in schema


@pytest.mark.parametrize("bad_sql", [
    "DROP TABLE accounts",
    "DELETE FROM accounts",
    "UPDATE accounts SET segment='x'",
    "INSERT INTO accounts VALUES (1)",
    "ALTER TABLE accounts ADD COLUMN x TEXT",
    "CREATE TABLE evil (x TEXT)",
    "PRAGMA table_info(accounts)",
])
def test_run_query_blocks_write_and_schema_statements(bad_sql):
    result = srv.run_query(bad_sql)
    assert result.startswith("Error:")


def test_run_query_blocks_stacked_statements():
    result = srv.run_query("SELECT 1; SELECT 2")
    assert "semicolon" in result.lower()


def test_run_query_blocks_non_select():
    result = srv.run_query("EXPLAIN SELECT * FROM accounts")
    assert result.startswith("Error:")


def test_run_query_allows_a_real_select():
    result = srv.run_query("SELECT COUNT(*) AS n FROM accounts")
    assert "n" in result
    assert "Error" not in result


def test_pipeline_summary_blended_row_sums_segments():
    text = srv.pipeline_summary()
    lines = {l.split(" | ")[0]: l for l in text.splitlines()[1:]}
    assert "BLENDED" in lines
    won_total = sum(int(lines[s].split(" | ")[1]) for s in ("SMB", "Mid-Market", "Enterprise"))
    blended_won = int(lines["BLENDED"].split(" | ")[1])
    assert won_total == blended_won


def test_pipeline_summary_segment_filter():
    text = srv.pipeline_summary(segment="SMB")
    assert "SMB" in text
    assert "Mid-Market" not in text
    assert "Enterprise" not in text


def test_mrr_trend_returns_requested_months():
    text = srv.mrr_trend("2026-01", "2026-03")
    for month in ("2026-01", "2026-02", "2026-03"):
        assert month in text


def test_arr_waterfall_buckets_sum_to_net_new():
    text = srv.arr_waterfall("2026-08")
    values = {}
    for line in text.splitlines():
        if ":" in line and "$" in line:
            label, amount = line.split(":", 1)
            sign = -1 if "-$" in amount else 1
            values[label.strip()] = sign * float(
                amount.replace("+$", "").replace("-$", "").replace(",", "").strip().split("%")[0]
                if "annualized" not in label else 0
            )
    new = values["new"]
    expansion = values["expansion"]
    contraction = values["contraction"]
    churned = values["churned"]
    net_new = values["net new MRR"]
    assert abs((new + expansion + contraction + churned) - net_new) < 0.01


def test_retention_grr_never_exceeds_nrr():
    """Mathematical invariant: GRR excludes expansion (capped at 100% per
    account), so it can never be higher than NRR for the same cohort."""
    text = srv.retention("2025-09", "2026-08")
    for line in text.splitlines():
        if "NRR=" in line and "GRR=" in line:
            nrr = float(line.split("NRR=")[1].split("%")[0])
            grr = float(line.split("GRR=")[1].split("%")[0])
            assert grr <= nrr + 0.01, line


def test_retention_unknown_segment_reports_no_accounts():
    text = srv.retention("2025-09", "2026-08", segment="Nonexistent")
    assert "no accounts" in text.lower()


def test_retention_decomposition_ordering():
    text = srv.retention_decomposition("2025-09", "2026-08", segment="Mid-Market", limit=3)
    assert "contracting/churned" in text
    assert "expanding" in text
    contracting_section = text.split("expanding")[0]
    deltas = [
        float(line.split(" | ")[-1])
        for line in contracting_section.splitlines()
        if line and line[0].isdigit()
    ]
    assert deltas == sorted(deltas)  # most negative first


def test_top_accounts_respects_limit():
    text = srv.top_accounts("2026-08", limit=3)
    data_rows = [l for l in text.splitlines()[2:] if l.strip()]
    assert len(data_rows) == 3


def test_get_account_detail_unknown_account():
    text = srv.get_account_detail(999999)
    assert "No account" in text


def test_get_account_detail_known_account_has_sections():
    any_account_id = int(srv.run_query(
        "SELECT account_id FROM accounts LIMIT 1"
    ).splitlines()[2])
    text = srv.get_account_detail(any_account_id)
    assert "monthly revenue" in text
    assert "support tickets" in text


def test_support_ticket_summary_has_both_breakdowns():
    text = srv.support_ticket_summary()
    assert "by category" in text
    assert "by priority" in text


def test_data_dictionary_mentions_every_table():
    text = srv.data_dictionary()
    for table in ("opportunities", "accounts", "channel_rates",
                   "monthly_usage", "support_tickets"):
        assert table in text


def test_gtm_business_review_prompt_references_the_period():
    text = srv.gtm_business_review("2026-08")
    assert "2026-08" in text
