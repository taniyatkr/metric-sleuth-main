# Example investigation (real, unedited run)

This is an actual run of `investigator.py`'s fully **open-ended**
`investigate()` loop against the flagship question — kept as documented
proof that capability works, not a curated or cherry-picked transcript.
Every number in Claude's final answer was independently re-verified with
a direct SQL query afterward (see the check below); all matched exactly.

**A note on how this fits the current project:** this run predates
`retention_drop_playbook.py`, the staged pipeline that now answers
question 1 by default (see the README's "two shapes of investigation"
section) — `investigator.py 1` no longer runs the loop below, it runs
that playbook. This transcript is kept because it's still true and still
useful: it's the real evidence that the open-ended agent, with zero
guidance about where to look, independently found essentially the same
root cause the staged playbook now proves deterministically (a Mid-
Market-specific billing_dispute spike concentrated in March/April
2026) — good confirmation that the two approaches converge on the same
answer, from two very different amounts of freedom. `investigate()`
itself is still fully present in `investigator.py` and still runs
exactly like this for any question that isn't one of the nine locked-in
ones.

Model: `claude-sonnet-5`. Guardrail: `MAX_ITERATIONS = 12` (raised from an
initial `8` after a first run showed it correctly narrowing all the way
to the root cause but running out of room to write the synthesis —
documented here rather than quietly bumping the number with no record of
why).

## Question

> Why did Mid-Market retention/NRR drop this year?

## Tool calls (11 iterations to a final answer)

The agent, unprompted, checked the schema, confirmed the data's date
range, then pulled retention for four different trailing-12-month windows
before deciding the trend itself needed explaining (not just the latest
number) — then moved to `retention_decomposition` to find which accounts
were driving it, `get_account_detail` to look at several of them
individually, `support_ticket_summary` and raw `run_query` calls to
correlate ticket volume against time and segment, and finally
`arr_waterfall` to confirm the company-wide financial impact lined up
with what it found at the account level:

```
get_schema()
run_query("SELECT MIN(month), MAX(month) FROM monthly_usage")
retention(base_month=2025-09, current_month=2026-09, segment=Mid-Market)  -> NRR 76.8% / GRR 70.0%
retention(base_month=2025-06, current_month=2026-06, segment=Mid-Market)  -> NRR 80.3% / GRR 73.4%
retention(base_month=2025-01, current_month=2026-01, segment=Mid-Market)  -> NRR 91.0% / GRR 81.3%
retention(base_month=2024-09, current_month=2025-09, segment=Mid-Market)  -> NRR 96.8% / GRR 87.1%
retention_decomposition(base_month=2025-09, current_month=2026-09, segment=Mid-Market, limit=15)
retention(base_month=2026-03, current_month=2026-09, segment=Mid-Market)
retention(base_month=2025-12, current_month=2026-06, segment=Mid-Market)
get_account_detail(account_id=199)   # Northgate Properties
get_account_detail(account_id=55)    # Amberwood Commerce
get_account_detail(account_id=71)    # Larkstead Marketplace
get_account_detail(account_id=153)   # Fernbrook Foods
get_account_detail(account_id=211)   # Pinebrook Payments
support_ticket_summary(start_month=2025-01, end_month=2025-12)
support_ticket_summary(start_month=2026-01, end_month=2026-09)
run_query(... billing_dispute tickets by year/segment/category ...)
run_query(... ticket counts by segment/year ...)
run_query(... Mid-Market ticket volume by month, with billing_dispute flag ...)
run_query(... billing_dispute tickets by month/segment, Jan-May 2026 ...)
get_account_detail(account_id=189)   # Novastead Foods
get_account_detail(account_id=45)    # Northpeak Realty
arr_waterfall(month=2026-03)
arr_waterfall(month=2026-04)
```

## Final answer (verbatim)

> **The trend itself:** Trailing-12-month Mid-Market retention has been
> sliding steadily all year — NRR 96.8% (base Sep'24), 91.0% (base
> Jan'25), 80.3% (base Jun'25), 76.8% (base Sep'25) — not one bad month,
> an accelerating deterioration.
>
> **Root cause — a billing incident concentrated in Mid-Market in
> March-April 2026:** billing_dispute tickets for Mid-Market accounts
> exploded from 3 all of 2025 to 75 in 2026, with 70 of those 75 (93%)
> landing in March (41) and April (29) alone. SMB billing disputes in the
> same two months were only 4 and 6 — a Mid-Market-specific spike, not
> company-wide. Resolution rate on these tickets also fell to 49.3% in
> 2026 vs. ~100% in 2025.
>
> This lines up with `arr_waterfall`, which shows March 2026 contraction
> of -$41,141.66 (vs. new $8,465 and expansion $9,699 that month) — by far
> the worst contraction month.
>
> **Account-level evidence:** 5 Mid-Market accounts churned to $0 between
> Sep'25 and Sep'26 (accounts 199, 55, 71, 153, 211; ~$36k of lost MRR
> combined), every one with a billing_dispute or cancellation_request
> ticket immediately preceding their revenue collapse. It wasn't only full
> churn: accounts that stayed also took permanent step-downs coincident
> with billing disputes — account 189 dropped 23% in March 2026 tied to
> tickets on Mar 7 and Apr 16; account 45 dropped 20% tied to an unresolved
> ticket on Mar 11.

## Independent re-verification

The claim "70 of 75 (93%) of Mid-Market billing_dispute tickets in 2026
landed in March/April" was checked with a direct query against
`data/metric_sleuth.db` after the run, not assumed correct:

```sql
-- Mid-Market billing_dispute tickets, all of 2026: 75
-- Mid-Market billing_dispute tickets, Mar+Apr 2026 only: 70 (93.3%)
```

Both figures matched exactly.

## What this run also demonstrated about the guardrail

A first attempt at this same question, with `MAX_ITERATIONS = 8`, hit the
cap one step before producing a final answer — it had already correctly
narrowed to "34 of 75 Mid-Market accounts had a billing dispute in
Feb-Apr 2026," which is the right evidence, it just needed a couple more
turns to write up the synthesis. That's the guardrail working as
designed (a runaway investigation gets stopped, not a silent failure) —
it just revealed the cap itself was tuned too tight for how thoroughly
this question gets investigated, which is why it's `12` now, with this
note as the record of why.
