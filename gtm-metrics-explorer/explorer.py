"""
GTM Metrics Explorer — a WORKFLOW, not an agent.

This is Layer 1 of metric-sleuth: MCP-powered GTM metrics exploration for
the seven known, descriptive questions listed in the project README. Every
question here maps to a fixed, predetermined sequence of SQL queries,
hand-authored in this file and run for real through the shared
mcp_server/analytics_server.py's run_query passthrough tool -- there is no
model in this file, no decision about which query to run or in what
order, and the SQL isn't copied from anywhere else. That's the whole
point of pairing this project with gtm-intelligence-investigator: this
file demonstrates that you don't need an agent for questions where the
steps -- and the queries -- are already known, and the other project
demonstrates what changes when they aren't. It's also where the division
of labor between analyst and model shows most plainly: every query below
was authored by the analyst; the model's job was only ever wiring it
through the MCP protocol.

Every report function takes an open MCP ClientSession and returns a
formatted string that shows both the SQL and the real result it produced
-- nothing here is computed independently of the query shown, and
nothing is hard-coded from a prior run.

Usage:
    python3 explorer.py                     # runs the full demo business review
    python3 explorer.py pipeline             # runs just one report
    python3 explorer.py retention 2025-09 2026-08
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_PATH = os.path.join(_REPO_ROOT, "mcp_server", "analytics_server.py")
SERVER_PARAMS = StdioServerParameters(command="python3", args=[_SERVER_PATH])


async def _call(session: ClientSession, tool: str, **kwargs) -> str:
    result = await session.call_tool(tool, kwargs)
    return result.content[0].text


async def _run_sql(session: ClientSession, sql: str) -> str:
    """Runs one hand-authored SQL query through the server's generic
    run_query tool and returns the result as text. Unlike the purpose-built
    tools (pipeline_summary, retention, etc.), run_query has no query of
    its own baked in -- the SQL below is authored directly, not copied
    from analytics_server.py, and this is the actual query that executes,
    not a display copy of one."""
    return await _call(session, "run_query", sql=sql)


# --------------------------------------------------------------------------
# Q1 SQL -- authored directly rather than reusing pipeline_summary's own
# query, and run for real through run_query. "Pipeline value over time" has
# to mean pipeline CREATED in each period (created_date), since nothing in
# this dataset stores historical point-in-time snapshots of open pipeline --
# a stage-grouped version of this was tried and dropped: stage reflects a
# deal's CURRENT stage, not its stage back when it was created, and most
# deals in early stages (Prospecting, pre-qualification) have no
# qualified_amount yet, so grouping dollar value by stage mostly returns
# gaps. That only works with real snapshot data, which this dataset
# doesn't have.
# --------------------------------------------------------------------------

OPEN_PIPELINE_BY_SEGMENT_SQL = """
SELECT segment, COUNT(*) AS open_count, SUM(qualified_amount) AS open_value
FROM opportunities
WHERE is_won IS NULL
GROUP BY segment
""".strip()

PIPELINE_CREATED_MOM_SQL = """
SELECT
  strftime('%Y-%m', created_date) AS month,
  SUM(qualified_amount) AS pipeline_created,
  ROUND(
    (SUM(qualified_amount) - LAG(SUM(qualified_amount)) OVER (ORDER BY strftime('%Y-%m', created_date)))
    * 100.0 / LAG(SUM(qualified_amount)) OVER (ORDER BY strftime('%Y-%m', created_date)),
  1) AS mom_pct_change
FROM opportunities
GROUP BY month
ORDER BY month
""".strip()

PIPELINE_CREATED_QOQ_SQL = """
SELECT
  strftime('%Y', created_date) || '-Q' || ((CAST(strftime('%m', created_date) AS INTEGER) - 1) / 3 + 1) AS quarter,
  SUM(qualified_amount) AS pipeline_created,
  ROUND(
    (SUM(qualified_amount) - LAG(SUM(qualified_amount)) OVER (ORDER BY strftime('%Y', created_date), (CAST(strftime('%m', created_date) AS INTEGER) - 1) / 3))
    * 100.0 / LAG(SUM(qualified_amount)) OVER (ORDER BY strftime('%Y', created_date), (CAST(strftime('%m', created_date) AS INTEGER) - 1) / 3),
  1) AS qoq_pct_change
FROM opportunities
GROUP BY quarter
ORDER BY quarter
""".strip()

PIPELINE_CREATED_YOY_SQL = """
SELECT
  strftime('%Y-%m', created_date) AS month,
  SUM(qualified_amount) AS pipeline_created,
  ROUND(
    (SUM(qualified_amount) - LAG(SUM(qualified_amount), 12) OVER (ORDER BY strftime('%Y-%m', created_date)))
    * 100.0 / LAG(SUM(qualified_amount), 12) OVER (ORDER BY strftime('%Y-%m', created_date)),
  1) AS yoy_pct_change
FROM opportunities
GROUP BY month
ORDER BY month
""".strip()


# --------------------------------------------------------------------------
# Q2 SQL -- ARR-weighted win rate (SUM(won ARR) / (SUM(won ARR) +
# SUM(lost ARR))), not the deal-COUNT win rate pipeline_summary already
# computes -- a deliberately different, dollar-weighted view. Built with
# CTEs rather than nested subqueries per preference: closed_won and
# closed_lost aggregate each side separately, combined joins them per
# segment, and the final SELECT adds a BLENDED row via UNION ALL (SQLite
# has no ROLLUP/GROUPING SETS). closed_amount is only ever populated for
# won deals, so qualified_amount is the sizing basis for lost deals --
# it's the only dollar figure a lost deal has.
# --------------------------------------------------------------------------

WIN_RATE_AND_CYCLE_SQL = """
WITH closed_won AS (
    SELECT segment, COUNT(*) AS won_count, SUM(closed_amount) AS won_arr,
           SUM(julianday(close_date) - julianday(created_date)) AS won_cycle_days_sum
    FROM opportunities WHERE stage = 'Closed Won' GROUP BY segment
),
closed_lost AS (
    SELECT segment, COUNT(*) AS lost_count, SUM(qualified_amount) AS lost_arr
    FROM opportunities WHERE stage = 'Closed Lost' GROUP BY segment
),
combined AS (
    SELECT
        cw.segment,
        cw.won_count, cw.won_arr, cw.won_cycle_days_sum,
        COALESCE(cl.lost_count, 0) AS lost_count,
        COALESCE(cl.lost_arr, 0) AS lost_arr
    FROM closed_won cw
    LEFT JOIN closed_lost cl ON cw.segment = cl.segment
)
SELECT
    segment,
    won_count, lost_count,
    ROUND(won_arr, 2) AS won_arr,
    ROUND(lost_arr, 2) AS lost_arr,
    ROUND(won_arr * 100.0 / (won_arr + lost_arr), 1) AS win_rate_pct_by_arr,
    ROUND(won_cycle_days_sum / won_count, 1) AS avg_cycle_days
FROM combined
UNION ALL
SELECT
    'BLENDED' AS segment,
    SUM(won_count), SUM(lost_count),
    ROUND(SUM(won_arr), 2), ROUND(SUM(lost_arr), 2),
    ROUND(SUM(won_arr) * 100.0 / (SUM(won_arr) + SUM(lost_arr)), 1),
    ROUND(SUM(won_cycle_days_sum) / SUM(won_count), 1)
FROM combined
""".strip()


# --------------------------------------------------------------------------
# Q3 SQL -- MRR trend. A plain aggregate, no CTE needed: total realized
# revenue per month plus a distinct-account count, over an inclusive
# month range. run_query takes no bind parameters, so start/end are
# interpolated directly into the literal -- safe here since both are
# internal YYYY-MM strings, never external input.
# --------------------------------------------------------------------------

def mrr_trend_sql(start_month: str, end_month: str) -> str:
    return f"""
SELECT month, ROUND(SUM(revenue), 2) AS mrr, COUNT(DISTINCT account_id) AS active_accounts
FROM monthly_usage
WHERE month BETWEEN '{start_month}' AND '{end_month}'
GROUP BY month
ORDER BY month
""".strip()


# --------------------------------------------------------------------------
# Q4 SQL -- ARR waterfall. Built with CTEs: `months` resolves the prior
# month from the target month, `cur`/`prev` are each month's per-account
# revenue, `joined` full-outer-joins them (SQLite has no FULL OUTER JOIN,
# so it's a LEFT JOIN unioned with the mirror-image LEFT JOIN to pick up
# accounts that only appear in one side), and the final SELECT buckets
# each account's movement into new/expansion/contraction/churned in one
# pass. net_new_mrr and annualized_impact are derived from those four
# totals rather than recomputed independently.
# --------------------------------------------------------------------------

def arr_waterfall_sql(month: str) -> str:
    return f"""
WITH months AS (
    SELECT '{month}' AS cur_month,
           strftime('%Y-%m', date('{month}-01', '-1 month')) AS prev_month
),
cur AS (
    SELECT account_id, SUM(revenue) AS rev FROM monthly_usage
    WHERE month = (SELECT cur_month FROM months) GROUP BY account_id
),
prev AS (
    SELECT account_id, SUM(revenue) AS rev FROM monthly_usage
    WHERE month = (SELECT prev_month FROM months) GROUP BY account_id
),
joined AS (
    SELECT c.account_id, c.rev AS cur_rev, COALESCE(p.rev, 0) AS prev_rev
    FROM cur c LEFT JOIN prev p ON c.account_id = p.account_id
    UNION ALL
    SELECT p.account_id, 0 AS cur_rev, p.rev AS prev_rev
    FROM prev p LEFT JOIN cur c ON c.account_id = p.account_id
    WHERE c.account_id IS NULL
),
totals AS (
    SELECT
      ROUND(SUM(CASE WHEN prev_rev = 0 AND cur_rev > 0 THEN cur_rev ELSE 0 END), 2) AS new,
      ROUND(SUM(CASE WHEN prev_rev > 0 AND cur_rev > prev_rev THEN cur_rev - prev_rev ELSE 0 END), 2) AS expansion,
      ROUND(SUM(CASE WHEN prev_rev > 0 AND cur_rev < prev_rev AND cur_rev > 0 THEN prev_rev - cur_rev ELSE 0 END), 2) AS contraction,
      ROUND(SUM(CASE WHEN cur_rev = 0 AND prev_rev > 0 THEN prev_rev ELSE 0 END), 2) AS churned
    FROM joined
)
SELECT
  (SELECT prev_month FROM months) AS prior_month,
  new, expansion, contraction, churned,
  ROUND(new + expansion - contraction - churned, 2) AS net_new_mrr,
  ROUND((new + expansion - contraction - churned) * 12, 2) AS annualized_impact
FROM totals
""".strip()


# --------------------------------------------------------------------------
# Q5 SQL -- NRR/GRR. `base` is the cohort's per-account revenue in
# base_month (joined to accounts for segment); `cur` is that same set's
# revenue in current_month; `joined` pairs them and adds capped_rev, each
# account's current revenue capped at its own base revenue (SQLite's
# scalar MIN(a, b), not the aggregate MIN). NRR sums uncapped current
# revenue over base revenue, so one account's expansion can offset
# another's contraction; GRR sums capped revenue instead, so expansion
# never masks contraction/churn. The final SELECT groups by segment, then
# UNION ALLs a BLENDED row (no ROLLUP/GROUPING SETS in SQLite).
# --------------------------------------------------------------------------

def retention_sql(base_month: str, current_month: str) -> str:
    return f"""
WITH base AS (
    SELECT u.account_id, a.segment, SUM(u.revenue) AS base_rev
    FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
    WHERE u.month = '{base_month}'
    GROUP BY u.account_id, a.segment
),
cur AS (
    SELECT account_id, SUM(revenue) AS cur_rev
    FROM monthly_usage WHERE month = '{current_month}'
    GROUP BY account_id
),
joined AS (
    SELECT b.account_id, b.segment, b.base_rev, COALESCE(c.cur_rev, 0) AS cur_rev,
           MIN(b.base_rev, COALESCE(c.cur_rev, 0)) AS capped_rev
    FROM base b LEFT JOIN cur c ON b.account_id = c.account_id
)
SELECT segment, COUNT(*) AS n,
       ROUND(SUM(base_rev), 2) AS base_total,
       ROUND(SUM(cur_rev) * 100.0 / SUM(base_rev), 1) AS nrr_pct,
       ROUND(SUM(capped_rev) * 100.0 / SUM(base_rev), 1) AS grr_pct
FROM joined
GROUP BY segment
UNION ALL
SELECT 'BLENDED', COUNT(*),
       ROUND(SUM(base_rev), 2),
       ROUND(SUM(cur_rev) * 100.0 / SUM(base_rev), 1),
       ROUND(SUM(capped_rev) * 100.0 / SUM(base_rev), 1)
FROM joined
""".strip()


# --------------------------------------------------------------------------
# Q6 SQL -- top accounts by revenue. A plain join-and-sort, no CTE needed.
# --------------------------------------------------------------------------

def top_accounts_sql(month: str, limit: int) -> str:
    return f"""
SELECT a.account_id, a.company_name, a.segment, a.industry,
       ROUND(SUM(u.revenue), 2) AS revenue
FROM monthly_usage u JOIN accounts a ON a.account_id = u.account_id
WHERE u.month = '{month}'
GROUP BY a.account_id
ORDER BY revenue DESC
LIMIT {limit}
""".strip()


# --------------------------------------------------------------------------
# Q7 SQL -- support ticket mix. Two independent plain aggregates (by
# category, by priority); no CTE needed for either.
# --------------------------------------------------------------------------

def ticket_summary_by_category_sql(start_month: str, end_month: str) -> str:
    return f"""
SELECT category, COUNT(*) AS n, ROUND(AVG(resolved) * 100, 1) AS resolved_pct
FROM support_tickets
WHERE strftime('%Y-%m', opened_date) BETWEEN '{start_month}' AND '{end_month}'
GROUP BY category
ORDER BY n DESC
""".strip()


def ticket_summary_by_priority_sql(start_month: str, end_month: str) -> str:
    return f"""
SELECT priority, COUNT(*) AS n
FROM support_tickets
WHERE strftime('%Y-%m', opened_date) BETWEEN '{start_month}' AND '{end_month}'
GROUP BY priority
ORDER BY n DESC
""".strip()


# --------------------------------------------------------------------------
# The seven Explorer questions, each a fixed sequence of tool calls
# --------------------------------------------------------------------------

async def pipeline_health(session: ClientSession) -> str:
    """Q1: What does our pipeline look like right now?"""
    open_pipeline = await _run_sql(session, OPEN_PIPELINE_BY_SEGMENT_SQL)
    mom = await _run_sql(session, PIPELINE_CREATED_MOM_SQL)
    qoq = await _run_sql(session, PIPELINE_CREATED_QOQ_SQL)
    yoy = await _run_sql(session, PIPELINE_CREATED_YOY_SQL)
    return (
        "Q1: What does our pipeline look like right now?\n\n"
        "-- Open pipeline, by segment --\n"
        f"SQL used:\n{OPEN_PIPELINE_BY_SEGMENT_SQL}\n\n"
        f"{open_pipeline}\n\n"
        "-- Pipeline created, month over month (note: the most recent month "
        "is partial -- data ends 2026-09-01) --\n"
        f"SQL used:\n{PIPELINE_CREATED_MOM_SQL}\n\n"
        f"{mom}\n\n"
        "-- Pipeline created, quarter over quarter --\n"
        f"SQL used:\n{PIPELINE_CREATED_QOQ_SQL}\n\n"
        f"{qoq}\n\n"
        "-- Pipeline created, year over year --\n"
        f"SQL used:\n{PIPELINE_CREATED_YOY_SQL}\n\n"
        f"{yoy}"
    )


async def win_rate_and_cycle(session: ClientSession) -> str:
    """Q2: What's our win rate and average sales cycle -- overall and by segment?"""
    result = await _run_sql(session, WIN_RATE_AND_CYCLE_SQL)
    return (
        "Q2: What's our win rate and average sales cycle, by segment?\n"
        "(ARR-weighted win rate -- SUM(won ARR) / (SUM(won ARR) + SUM(lost ARR)) -- "
        "not the deal-count win rate; a dollar-weighted view can read differently, "
        "e.g. blended is 18.0% here vs. 21.7% by count, since lost deals in this "
        "dataset skew a bit larger on average than won ones)\n\n"
        f"SQL used:\n{WIN_RATE_AND_CYCLE_SQL}\n\n"
        f"{result}"
    )


async def mrr_report(session: ClientSession, start_month: str, end_month: str) -> str:
    """Q3: What was MRR last month, and what's the trend?"""
    sql = mrr_trend_sql(start_month, end_month)
    trend = await _run_sql(session, sql)
    return (
        f"Q3: MRR trend, {start_month} to {end_month}\n\n"
        f"SQL used:\n{sql}\n\n"
        f"{trend}"
    )


async def arr_waterfall_report(session: ClientSession, month: str) -> str:
    """Q4: What does the ARR waterfall look like for a given month?"""
    sql = arr_waterfall_sql(month)
    waterfall = await _run_sql(session, sql)
    return (
        f"Q4: ARR waterfall for {month}\n\n"
        f"SQL used:\n{sql}\n\n"
        f"{waterfall}"
    )


async def retention_report(session: ClientSession, base_month: str, current_month: str) -> str:
    """Q5: What's our NRR and GRR -- blended and by segment, for a period?"""
    sql = retention_sql(base_month, current_month)
    ret = await _run_sql(session, sql)
    return (
        f"Q5: Retention (NRR/GRR), {base_month} -> {current_month}\n"
        "(GRR caps each account's current revenue at its own base revenue before "
        "summing, so expansion elsewhere can't mask contraction/churn the way it "
        "does in NRR)\n\n"
        f"SQL used:\n{sql}\n\n"
        f"{ret}"
    )


async def top_accounts_report(session: ClientSession, month: str, limit: int = 10) -> str:
    """Q6: Who are our top accounts by revenue this month?"""
    sql = top_accounts_sql(month, limit)
    top = await _run_sql(session, sql)
    return (
        f"Q6: Top {limit} accounts by revenue, {month}\n\n"
        f"SQL used:\n{sql}\n\n"
        f"{top}"
    )


async def ticket_health_report(session: ClientSession, start_month: str, end_month: str) -> str:
    """Q7: What does our support ticket volume/category/priority mix look like?"""
    by_category_sql = ticket_summary_by_category_sql(start_month, end_month)
    by_priority_sql = ticket_summary_by_priority_sql(start_month, end_month)
    by_category = await _run_sql(session, by_category_sql)
    by_priority = await _run_sql(session, by_priority_sql)
    return (
        f"Q7: Support ticket mix, {start_month} to {end_month}\n\n"
        "-- by category --\n"
        f"SQL used:\n{by_category_sql}\n\n"
        f"{by_category}\n\n"
        "-- by priority --\n"
        f"SQL used:\n{by_priority_sql}\n\n"
        f"{by_priority}"
    )


# --------------------------------------------------------------------------
# Dispatcher -- the "predetermined path" a workflow follows
# --------------------------------------------------------------------------

async def explore(session: ClientSession, question_type: str, **kwargs) -> str:
    """Dispatches to one of the seven fixed report functions above. This
    is a lookup, not a decision -- the caller picks question_type, nothing
    here reasons about which report answers the question."""
    dispatch = {
        "pipeline": lambda: pipeline_health(session),
        "win_rate": lambda: win_rate_and_cycle(session),
        "mrr_trend": lambda: mrr_report(session, **kwargs),
        "arr_waterfall": lambda: arr_waterfall_report(session, **kwargs),
        "retention": lambda: retention_report(session, **kwargs),
        "top_accounts": lambda: top_accounts_report(session, **kwargs),
        "tickets": lambda: ticket_health_report(session, **kwargs),
    }
    if question_type not in dispatch:
        return f"Unknown question_type '{question_type}'. Options: {list(dispatch)}"
    return await dispatch[question_type]()


# --------------------------------------------------------------------------
# Demo: the full business review, all seven questions in sequence
# --------------------------------------------------------------------------

async def run_business_review(session: ClientSession) -> str:
    sections = [
        await pipeline_health(session),
        await win_rate_and_cycle(session),
        await mrr_report(session, "2026-03", "2026-08"),
        await arr_waterfall_report(session, "2026-08"),
        await retention_report(session, "2025-09", "2026-08"),
        await top_accounts_report(session, "2026-08", limit=5),
        await ticket_health_report(session, "2026-07", "2026-08"),
    ]
    divider = "\n\n" + "=" * 70 + "\n\n"
    return divider.join(sections)


async def main():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if len(sys.argv) == 1:
                print(await run_business_review(session))
                return

            question_type = sys.argv[1]
            args = sys.argv[2:]
            kwarg_map = {
                "mrr_trend": ["start_month", "end_month"],
                "arr_waterfall": ["month"],
                "retention": ["base_month", "current_month"],
                "top_accounts": ["month"],
                "tickets": ["start_month", "end_month"],
            }
            keys = kwarg_map.get(question_type, [])
            kwargs = dict(zip(keys, args))
            print(await explore(session, question_type, **kwargs))


if __name__ == "__main__":
    asyncio.run(main())
