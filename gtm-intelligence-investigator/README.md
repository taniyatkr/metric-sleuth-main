# Metric Sleuth: GTM Intelligence Investigator

## Agentic GTM Investigation with MCP

This is Layer 2 of **metric-sleuth**. It answers open-ended GTM questions
using the same shared MCP server
[`gtm-metrics-explorer`](../gtm-metrics-explorer/README.md) queries — but
where Explorer's SQL is fixed in advance for known questions, this layer
exists for the harder case: questions where the right sequence of queries
genuinely isn't knowable ahead of time. That's the contrast this whole
repo is built to demonstrate, and this project pushes it one level
further, because it actually contains **two different shapes of
investigation**, chosen deliberately for two different situations:

- **`investigate(question)`** — a genuine, fully open-ended **agent**.
  Claude gets the shared MCP tool list and decides, step by step, what to
  call next and when it has enough evidence to stop. Nothing about the
  path is fixed ahead of time. This is the right tool for a question
  nobody has asked before.
- **Nine staged playbooks** (`retention_drop_playbook.py`,
  `q2_mid_market_underperformance.py`, … `q9_source_rep_quality.py`) —
  each a **fixed, analyst-defined sequence** of deterministic code
  stages, run in order, with early stops when a stage's rule is
  satisfied. Claude's role in each is narrowed to exactly one bounded
  synthesis stage at the end: no tools, no queries, reasoning only over
  the evidence dict the code stages already assembled. This is the right
  tool once you know which nine questions you're going to keep asking,
  and you want the answer to be reproducible and its thresholds
  auditable, not re-derived from scratch on every run.

Why both exist in one project, rather than just building the second kind
straight through: building these nine with the fully open-ended agent
first is exactly what surfaced the mistake the staged versions now guard
against. An early pass — both the agent's own reasoning and, independently,
one playbook's first draft of the same check — summed raw usage `units`
across incompatible channel types (SMS messages, voice minutes, email
sends, verification checks) and read the resulting channel-mix shift as
a price increase that had never actually happened (`channel_rates` turned
out to be a static table with no time dimension at all — see
`../data/METHODOLOGY.md` for the full story). A dynamic agent re-derives
its whole approach every run, including whatever mistake is easiest to
make again. A staged playbook fixes the *correct* approach once, in code,
after an analyst has actually caught and corrected that mistake — and
every later run inherits the fix for free. For a genuinely novel
question, that rigor doesn't exist yet to fix in place, so the open-ended
loop is still the only honest option; for a known, recurring question,
the staged version is strictly better.

## The nine questions

1. **Why did Mid-Market retention/NRR drop this year?** — the flagship
   question, and the one that originated the channel-mixing bug fix
   above. There's a real, planted answer in the data (see
   `../data/METHODOLOGY.md`) for the playbook to actually trace and
   prove, not just describe. 6 stages, `retention_drop_playbook.py`.
2. **What's driving the Mid-Market segment's underperformance?** — your
   own analyst checklist (outlier check, seasonality, source/rep
   concentration in place of region, ACV, deal volume/SQO, win rate,
   landing skew), 8 stages. Real finding: Mid-Market closed just 1 of 34
   deals in 2026-Q2 — a 2.9% win rate against ~19-23% in the comparison
   quarters — while deal volume, ACV, and pipeline aging all came back
   clean. `q2_mid_market_underperformance.py`.
3. Why are we seeing a spike in billing complaints this spring? 7 stages,
   shares the rate-stability check with question 1.
   `q3_billing_complaint_spike.py`.
4. What's driving our overall NRR this quarter? *(decomposition, not an
   anomaly hunt — works for any period)* 5 stages; hands off to question
   1's full playbook when the worst segment is Mid-Market, rather than
   re-deriving the same investigation. `q4_overall_nrr.py`.
5. Which accounts are at risk of churning right now? *(a ranking, not a
   single narrative — no Claude stage)* every active account scored on 3
   signals. `q5_churn_risk_scoring.py`.
6. What typically precedes a cancellation for our customers? *(pattern-
   mining with a control group — without one you can't tell if a
   "precursor" is actually predictive or just common anyway)*
   `q6_cancellation_precursors.py`.
7. Is account X showing warning signs? *(single-account drill-down,
   deterministic — no Claude stage)* `q7_account_watch_check.py`.
8. Why is our win rate lower this quarter than last? *(genuinely open —
   no seeded answer; includes a mix-shift/Simpson's-paradox check)*
   `q8_win_rate_drop.py`.
9. Which source or rep is producing the best-quality pipeline? *(a
   ranking by composite score — win rate, ACV, cycle time)*
   `q9_source_rep_quality.py`.

Questions 1–3 share the same real root cause; 4–7 draw on the general,
real (but not singular) churn/ticket correlation; 8–9 are honest
open-ended analysis on real variance, with no planted "aha" — worth being
upfront about that distinction rather than implying every answer traces
to a hidden cause.

## Running it

**A key is needed only where a Claude call actually happens.** For the
nine staged playbooks, every diagnostic stage is plain deterministic code
against the real database and needs no key at all — only each playbook's
one final synthesis stage does, and it fails soft (prints "skipped:
ANTHROPIC_API_KEY not set" and returns the rest of the evidence) rather
than blocking the run if the key is missing. `investigate()`'s open-ended
loop is different: every single step is a Claude call, so it genuinely
needs a key to do anything at all.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # only required for a full loop run;
                                        # the 9 playbooks run their diagnostic
                                        # stages fine without it

cd gtm-intelligence-investigator
python3 investigator.py           # flagship question (#1) by default -- runs its staged playbook
python3 investigator.py 7         # any of the 9 -- runs that question's staged playbook
python3 investigator.py "Why did SMB win rate drop?"   # anything else -- runs the real open-ended loop

# each staged playbook is also runnable directly:
python3 retention_drop_playbook.py
python3 q2_mid_market_underperformance.py
```

## How it works

### The open-ended loop (`investigate()`)

`investigator.py` opens a real MCP client session against the shared
`mcp_server/analytics_server.py`, pulls the live tool list via
`list_tools()`, and translates it into Anthropic's tool schema format —
so the agent's available tools can never drift out of sync with what the
server actually offers. Then it runs the standard agentic tool-use loop:

1. Send the question + tool list to Claude.
2. If `stop_reason` is `tool_use`, actually execute that tool call against
   the real MCP server, feed the result back as a `tool_result`, and ask
   again.
3. Repeat until `stop_reason` is `end_turn` (Claude has enough evidence to
   answer), or a hard cap of 12 iterations is hit — a guardrail so a bad
   loop can't run away, not a soft suggestion. (Raised from an original
   cap of 8 after a live run showed the loop correctly narrowing all the
   way to the root cause but running out of room to write up the final
   answer — see `../data/METHODOLOGY.md`.)

The system prompt requires every claim in the final answer to cite a
specific number or result actually retrieved through a tool call in that
conversation — not a plausible-sounding guess. Run with the default
`verbose=True` and you'll see every tool call and result print as it
happens, so the investigation's evidence trail is visible, not just its
conclusion. `main()` routes to this loop for anything that isn't one of
the nine locked-in demo questions.

### The nine staged playbooks

Each playbook file is a small, self-contained pipeline: a locked stage
order (documented in a comment block at the top of the file), a plain
Python function per stage with the threshold constants it uses declared
right above it and a `RULE:` comment explaining what it checks and why,
and a `run_*()` function that executes every stage in order, prints its
findings as it goes, and stops early wherever a stage's `stop_here` flag
says the investigation is already answered (e.g. "this is seasonal,"
"one account explains almost all of the loss," "the drop isn't even
real"). Every stage's output is added to a running `evidence` dict; the
final stage hands that whole dict to a bounded Claude call
(`playbook_lib.claude_synthesize`) with no tools and no ability to query
anything else — it can only write up what the code already found.
`playbook_lib.py` holds the handful of functions more than one playbook
needs verbatim (revenue/usage totals by segment, the corrected usage-drop
check, NRR/GRR and ARR-waterfall math that matches
`analytics_server.py`'s own tools exactly, and the common-attribute
gatherer + synthesis call every final stage uses); anything specific to
one question's own stages lives in that question's file, not shared.

## What's verified here vs. what you'll verify

Every staged playbook's diagnostic stages (everything except each one's
final Claude call) were run against the real database while building
this — the numbers printed in each file's own module docstring are real
output, not illustrative examples. The tool-schema translation and MCP
connection for the open-ended loop were also tested live against the
real server. What still needs a real Anthropic API key to see for
yourself: every playbook's own synthesis stage, and the full open-ended
loop's actual behavior on a freeform question — whether it reliably lands
on the right answer without being told, and how many iterations it
takes. That first real run is yours to do, and worth watching closely: if
a loop run doesn't converge cleanly, that's useful signal about the
system prompt or the guardrail cap, not a sign something is broken.
