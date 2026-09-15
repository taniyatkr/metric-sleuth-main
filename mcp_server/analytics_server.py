"""
metric-sleuth — Wavemark GTM analytics MCP server

Exposes the Wavemark dataset (opportunities, accounts, channel_rates,
monthly_usage, support_tickets) to any MCP client through a small set of
tools, one resource, and one prompt. Both gtm-metrics-explorer (a fixed
workflow) and gtm-intelligence-investigator (an agent) call these same
tools -- the difference between the two projects is never the data access
layer, it's whether the caller follows one predetermined path through
these tools or decides its own path step by step.

Every tool computes its answer fresh from the raw tables at call time.
Nothing here reads a precomputed metric or a stored flag -- NRR, GRR,
churn status, and the planted incident are all things a caller has to
derive, the same way a real analyst would.
"""

import os
import re
import sqlite3

from mcp.server.fastmcp import FastMCP

# Resolved relative to this file, not the caller's cwd -- this server gets
# spawned as a subprocess by explorer.py, investigator.py, and tests from
# different working directories, so a plain relative path would break
# depending on where the caller happens to run from.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "metric_sleuth.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_table(rows: list[sqlite3.Row]) -> str:
    """Turns a list of sqlite3.Row into a simple, readable text table."""
    if not rows:
        return "(no rows)"
    cols = rows[0].keys()
    widths = [max(len(c), max(len(str(r[c])) for r in rows)) for c in cols]
    lines = [" | ".join(c.ljust(w) for c, w in zip(cols, widths))]
    lines.append("-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(" | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    return "\n".join(lines)


mcp = FastMCP("wavemark-gtm-analytics")


# ---------------------------------------------------------------------------
# Schema + safe free-form query
# ---------------------------------------------------------------------------

@mcp.tool()
def get_schema() -> str:
    """Lists every table in the Wavemark database and each table's columns
    with its SQLite type. Call this first if you don't already know the
    schema -- it's the fastest way to see what's queryable before writing
    a run_query call."""
    conn = _connect()
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    out = []
    for t in tables:
        out.append(f"{t}:")
        for col in conn.execute(f"PRAGMA table_info({t})"):
            out.append(f"  - {col['name']} ({col['type']})")
    conn.close()
    return "\n".join(out)


_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
)


@mcp.tool()
def run_query(sql: str) -> str:
    """Runs a single, read-only SELECT query against the Wavemark database
    and returns the result as a text table. A query may start with SELECT
    or with a WITH ... AS (...) common-table-expression, as long as it's
    still read-only overall -- no semicolons (one statement per call), and
    no INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, or other
    schema/data-modifying keywords anywhere in the query. Use get_schema
    first if you need to see table and column names."""
    stripped = sql.strip()
    if ";" in stripped:
        return "Error: semicolons aren't allowed -- one SELECT statement per call."
    # FIXED: originally required the query to literally start with
    # "SELECT", which rejected every CTE (a WITH ... AS (...) SELECT ...)
    # even though it's exactly as read-only -- the forbidden-keyword check
    # below is still what actually guards against a WITH clause feeding
    # into INSERT/UPDATE/DELETE, so allowing the WITH prefix doesn't
    # loosen the real safety check at all.
    if not stripped.upper().startswith(("SELECT", "WITH")):
        return "Error: only SELECT statements (or WITH ... AS (...) SELECT ...) are allowed."
    # FIXED: was a plain substring check (`kw in upper`), which blocked
    # perfectly safe queries -- e.g. a column named created_date contains
    # "CREATE" as a substring, so any query referencing it (like a
    # created_date-based trend) was rejected even though it's a read-only
    # SELECT touching nothing but that column. Word-boundary matching so
    # only the actual keyword token trips the filter, not a keyword
    # embedded inside a longer identifier.
    upper = stripped.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return f"Error: the keyword '{kw}' isn't allowed in run_query."

    conn = _connect()
    try:
        rows = conn.execute(stripped).fetchall()
    except sqlite3.Error as e:
        return f"SQL error: {e}"
    finally:
        conn.close()
    return _rows_to_table(rows)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@mcp.tool()
def pipeline_summary(segment: str | None = None) -> str:
    """Summarizes the sales pipeline: win rate and average sales cycle
    length (in days) for resolved opportunities, plus the count and total
    value of opportunities still open, broken down by segment (SMB,
    Mid-Market, Enterprise). Pass segment to filter to just one of those;
    omit it for all three plus a blended row."""
    conn = _connect()
    seg_filter = "WHERE segment = ?" if segment else ""
    params = (segment,) if segment else ()

    resolved = conn.execute(f"""
        SELECT segment,
               SUM(CASE WHEN is_won=1 THEN 1 ELSE 0 END) AS won,
               SUM(CASE WHEN is_won=0 THEN 1 ELSE 0 END) AS lost,
               ROUND(AVG(julianday(close_date) - julianday(created_date)), 1) AS avg_cycle_days
        FROM opportunities
        {seg_filter} {"AND" if seg_filter else "WHERE"} is_won IS NOT NULL
        GROUP BY segment
    """, params).fetchall()

    open_deals = conn.execute(f"""
        SELECT segment, COUNT(*) AS open_count, ROUND(SUM(qualified_amount), 2) AS open_value
        FROM opportunities
        {seg_filter} {"AND" if seg_filter else "WHERE"} is_won IS NULL
        GROUP BY segment
    """, params).fetchall()
    conn.close()

    open_by_seg = {r["segment"]: r for r in open_deals}
    lines = ["segment | won | lost | win_rate | avg_cycle_days | open_count | open_value"]
    tot_won = tot_lost = tot_open_count = 0
    tot_open_value = 0.0
    for r in resolved:
        win_rate = r["won"] / (r["won"] + r["lost"]) * 100 if (r["won"] + r["lost"]) else 0
        o = open_by_seg.get(r["segment"])
        lines.append(
            f"{r['segment']} | {r['won']} | {r['lost']} | {win_rate:.1f}% | "
            f"{r['avg_cycle_days']} | {o['open_count'] if o else 0} | "
            f"{o['open_value'] if o else 0}"
        )
        tot_won += r["won"]; tot_lost += r["lost"]
        tot_open_count += o["open_count"] if o else 0
        tot_open_value += o["open_value"] if o else 0
    if not segment and (tot_won + tot_lost):
        lines.append(
            f"BLENDED | {tot_won} | {tot_lost} | {tot_won/(tot_won+tot_lost)*100:.1f}% | "
            f"— | {tot_open_count} | {round(tot_open_value, 2)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Realized revenue
# ---------------------------------------------------------------------------

@mcp.tool()
def mrr_trend(start_month: str, end_month: str) -> str:
    """Returns total realized monthly revenue (MRR) for each month from
    start_month to end_month inclusive, e.g. start_month='2026-01',
    end_month='2026-08'. Months use the 'YYYY-MM' format."""
    conn = _connect()
    rows = conn.execute("""
        SELECT month, ROUND(SUM(revenue), 2) AS mrr, COUNT(DISTINCT account_id) AS active_accounts
        FROM monthly_usage
        WHERE month BETWEEN ? AND ?
        GROUP BY month ORDER BY month
    """, (start_month, end_month)).fetchall()
    conn.close()
    return _rows_to_table(rows)


@mcp.tool()
def arr_waterfall(month: str) -> str:
    """Breaks down one month's revenue movement vs. the prior month into
    new, expansion, contraction, and churned dollars (a standard ARR
    waterfall), plus the resulting net new MRR and its annualized (x12)
    impact. month uses 'YYYY-MM' format; the prior month is inferred
    automatically. 'New' includes both genuinely new signups and any
    account reactivating after a gap -- this dataset doesn't distinguish
    the two."""
    conn = _connect()
    prev_month_row = conn.execute(
        "SELECT strftime('%Y-%m', date(? || '-01', '-1 month')) AS m", (month,)
    ).fetchone()
    prev_month = prev_month_row["m"]

    cur = {r["account_id"]: r["rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS rev FROM monthly_usage WHERE month=? GROUP BY account_id",
        (month,)
    )}
    prev = {r["account_id"]: r["rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS rev FROM monthly_usage WHERE month=? GROUP BY account_id",
        (prev_month,)
    )}
    conn.close()

    new = expansion = contraction = churned = 0.0
    for acct, rev in cur.items():
        prev_rev = prev.get(acct, 0.0)
        if prev_rev == 0:
            new += rev
        elif rev > prev_rev:
            expansion += rev - prev_rev
        elif rev < prev_rev:
            contraction += prev_rev - rev
    for acct, prev_rev in prev.items():
        if acct not in cur:
            churned += prev_rev

    net_new = new + expansion - contraction - churned
    lines = [
        f"month: {month} (vs. {prev_month})",
        f"new:         +${new:,.2f}",
        f"expansion:   +${expansion:,.2f}",
        f"contraction: -${contraction:,.2f}",
        f"churned:     -${churned:,.2f}",
        f"net new MRR: {'+' if net_new >= 0 else '-'}${abs(net_new):,.2f}",
        f"annualized (x12) impact: {'+' if net_new >= 0 else '-'}${abs(net_new) * 12:,.2f}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def _retention_cohort(conn, base_month: str, current_month: str, segment: str | None):
    seg_join = "JOIN accounts a ON a.account_id = u.account_id"
    seg_filter = "AND a.segment = ?" if segment else ""
    params = (base_month,) + ((segment,) if segment else ())

    base = conn.execute(f"""
        SELECT u.account_id, a.segment, SUM(u.revenue) AS base_rev
        FROM monthly_usage u {seg_join}
        WHERE u.month = ? {seg_filter}
        GROUP BY u.account_id, a.segment
    """, params).fetchall()

    cur = {r["account_id"]: r["cur_rev"] for r in conn.execute(
        "SELECT account_id, SUM(revenue) AS cur_rev FROM monthly_usage WHERE month=? GROUP BY account_id",
        (current_month,)
    )}
    return base, cur


@mcp.tool()
def retention(base_month: str, current_month: str, segment: str | None = None) -> str:
    """Computes Net Revenue Retention (NRR) and Gross Revenue Retention
    (GRR) for the cohort of accounts that had usage in base_month,
    comparing their revenue in current_month. Pass segment to scope to
    one segment; omit for blended plus a per-segment breakdown. Months use
    'YYYY-MM' format. NRR/GRR aren't stored anywhere -- this always
    computes them fresh from monthly_usage."""
    conn = _connect()
    base, cur = _retention_cohort(conn, base_month, current_month, None)
    conn.close()

    def summarize(rows):
        base_total = sum(r["base_rev"] for r in rows)
        if base_total == 0:
            return None
        cur_total = sum(cur.get(r["account_id"], 0.0) for r in rows)
        capped_total = sum(min(r["base_rev"], cur.get(r["account_id"], 0.0)) for r in rows)
        return len(rows), base_total, cur_total / base_total * 100, capped_total / base_total * 100

    lines = [f"cohort: accounts active in {base_month}, measured again in {current_month}"]
    by_segment = {}
    for r in base:
        by_segment.setdefault(r["segment"], []).append(r)

    if segment:
        rows = by_segment.get(segment, [])
        s = summarize(rows)
        if s:
            n, b, nrr, grr = s
            lines.append(f"{segment}: n={n} base=${b:,.0f} NRR={nrr:.1f}% GRR={grr:.1f}%")
        else:
            lines.append(f"{segment}: no accounts with usage in {base_month}")
        return "\n".join(lines)

    for seg, rows in sorted(by_segment.items()):
        s = summarize(rows)
        if s:
            n, b, nrr, grr = s
            lines.append(f"{seg}: n={n} base=${b:,.0f} NRR={nrr:.1f}% GRR={grr:.1f}%")
    s_all = summarize(base)
    if s_all:
        n, b, nrr, grr = s_all
        lines.append(f"BLENDED: n={n} base=${b:,.0f} NRR={nrr:.1f}% GRR={grr:.1f}%")
    return "\n".join(lines)


@mcp.tool()
def retention_decomposition(
    base_month: str, current_month: str, segment: str | None = None, limit: int = 10
) -> str:
    """Shows WHICH accounts are driving NRR/GRR for a cohort -- the
    biggest expanding accounts and the biggest contracting/churned
    accounts by dollar swing, between base_month and current_month. Use
    this after retention() to explain a number, not just report it. Pass
    segment to scope to one segment."""
    conn = _connect()
    base, cur = _retention_cohort(conn, base_month, current_month, segment)
    conn.close()

    deltas = []
    for r in base:
        cur_rev = cur.get(r["account_id"], 0.0)
        deltas.append({
            "account_id": r["account_id"], "segment": r["segment"],
            "base_rev": r["base_rev"], "cur_rev": cur_rev,
            "delta": cur_rev - r["base_rev"],
        })
    deltas.sort(key=lambda d: d["delta"])

    lines = [f"cohort: {base_month} -> {current_month}" + (f" ({segment})" if segment else "")]
    lines.append(f"-- top {limit} contracting/churned accounts --")
    lines.append("account_id | segment | base_rev | cur_rev | delta")
    for d in deltas[:limit]:
        lines.append(f"{d['account_id']} | {d['segment']} | {d['base_rev']:.2f} | "
                      f"{d['cur_rev']:.2f} | {d['delta']:.2f}")
    lines.append(f"-- top {limit} expanding accounts --")
    lines.append("account_id | segment | base_rev | cur_rev | delta")
    for d in sorted(deltas, key=lambda d: -d["delta"])[:limit]:
        lines.append(f"{d['account_id']} | {d['segment']} | {d['base_rev']:.2f} | "
                      f"{d['cur_rev']:.2f} | {d['delta']:.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Accounts + tickets
# ---------------------------------------------------------------------------

@mcp.tool()
def top_accounts(month: str, limit: int = 10) -> str:
    """Returns the top accounts by realized revenue in a given month
    ('YYYY-MM' format), with segment, industry, and company name."""
    conn = _connect()
    rows = conn.execute("""
        SELECT a.account_id, a.company_name, a.segment, a.industry,
               ROUND(SUM(u.revenue), 2) AS revenue
        FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
        WHERE u.month = ?
        GROUP BY a.account_id ORDER BY revenue DESC LIMIT ?
    """, (month, limit)).fetchall()
    conn.close()
    return _rows_to_table(rows)


@mcp.tool()
def get_account_detail(account_id: int) -> str:
    """Returns everything about one account: its profile (segment,
    industry, primary channel, signup date), its monthly revenue for
    every month it's had usage, and its full support ticket history.
    Use this to drill into a specific account, e.g. when investigating
    whether it's showing warning signs of churn."""
    conn = _connect()
    acct = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if not acct:
        conn.close()
        return f"No account with account_id={account_id}"

    usage = conn.execute("""
        SELECT month, ROUND(SUM(revenue), 2) AS revenue
        FROM monthly_usage WHERE account_id = ? GROUP BY month ORDER BY month
    """, (account_id,)).fetchall()
    tickets = conn.execute("""
        SELECT opened_date, category, priority, resolved
        FROM support_tickets WHERE account_id = ? ORDER BY opened_date
    """, (account_id,)).fetchall()
    conn.close()

    lines = [
        f"account_id={acct['account_id']} company={acct['company_name']} "
        f"segment={acct['segment']} industry={acct['industry']} "
        f"primary_channel={acct['primary_channel']} signup_date={acct['signup_date']}",
        "",
        "-- monthly revenue --",
        _rows_to_table(usage),
        "",
        "-- support tickets --",
        _rows_to_table(tickets),
    ]
    return "\n".join(lines)


@mcp.tool()
def support_ticket_summary(start_month: str | None = None, end_month: str | None = None) -> str:
    """Summarizes support ticket volume by category and priority, and the
    resolved rate. Optionally scope to a date range with start_month and
    end_month ('YYYY-MM' format); omit both for all-time."""
    conn = _connect()
    where = ""
    params: tuple = ()
    if start_month and end_month:
        where = "WHERE strftime('%Y-%m', opened_date) BETWEEN ? AND ?"
        params = (start_month, end_month)

    by_category = conn.execute(f"""
        SELECT category, COUNT(*) AS n, ROUND(AVG(resolved)*100, 1) AS resolved_pct
        FROM support_tickets {where} GROUP BY category ORDER BY n DESC
    """, params).fetchall()
    by_priority = conn.execute(f"""
        SELECT priority, COUNT(*) AS n FROM support_tickets {where} GROUP BY priority ORDER BY n DESC
    """, params).fetchall()
    conn.close()

    return "\n".join([
        "-- by category --", _rows_to_table(by_category),
        "", "-- by priority --", _rows_to_table(by_priority),
    ])


# ---------------------------------------------------------------------------
# Resource + prompt
# ---------------------------------------------------------------------------

@mcp.resource("data://dictionary")
def data_dictionary() -> str:
    """Plain-English description of every table and column in the
    Wavemark database, for a client that wants to understand the schema
    without guessing from column names alone."""
    return """
Wavemark GTM data dictionary

opportunities -- the B2B sales pipeline.
  opportunity_id, segment (SMB/Mid-Market/Enterprise), source (Outbound/
  Inbound/Partner), rep, created_date, qualified_date/qualified_amount
  (null if never qualified), stage, close_date, closed_amount (actual
  deal size at close, only for won deals), is_won (1/0/NULL if still open)

accounts -- Wavemark's customers, one row per WON opportunity.
  account_id, opportunity_id, company_name, segment, industry,
  signup_date, primary_channel (sms/voice/email/verification),
  starting_mrr

channel_rates -- list price per unit, by channel.
  channel, unit_name, list_price_usd

monthly_usage -- realized, consumption-based revenue.
  account_id, month (YYYY-MM), channel, units, revenue

support_tickets -- fully synthetic ticket history.
  ticket_id, account_id, opened_date, category, priority, resolved (1/0)

No table stores churn status, NRR/GRR, or any incident flag -- all of
those are computed from the raw rows above, at query time.
"""


@mcp.prompt()
def gtm_business_review(period: str) -> str:
    """Generates instructions for a GTM business review covering pipeline,
    revenue, retention, and support signal for a given period."""
    return f"""
Prepare a GTM business review for {period} using the Wavemark analytics
tools. Cover, in order:

1. Pipeline: call pipeline_summary() -- report win rate and average
   sales cycle by segment, and current open pipeline value.
2. Revenue: call mrr_trend() for the last 6 months ending in {period},
   then arr_waterfall({period}) for the new/expansion/contraction/churned
   breakdown.
3. Retention: call retention() comparing {period} minus 12 months to
   {period}, blended and by segment. If any segment looks off, call
   retention_decomposition() to see which accounts are driving it.
4. Support signal: call support_ticket_summary() for the last 2 months
   and flag any category or priority spike.

Summarize with one clear takeaway per section, and one overall headline
for the period.
"""


if __name__ == "__main__":
    mcp.run()
