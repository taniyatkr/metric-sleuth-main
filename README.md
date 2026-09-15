# metric-sleuth

Two ways to answer GTM questions about the same data, built to show the
difference on purpose: a fixed **workflow** for questions where the steps
are already known, and a genuine **agent** for questions where they
aren't.

The data is real in structure, synthetic in content: **Wavemark**, a
fictional B2B communications-API company (SMS/voice/email/verification,
billed by consumption — modeled on the real shape of companies like
Twilio, not a copy of any real one), with a full sales pipeline, usage-based
realized revenue, and a support-ticket history — including one incident
deliberately planted in the data for the agent layer to actually find,
not be told about. Every number in this repo is either sourced from a
cited industry benchmark or independently verified after generation with
real SQL — see [`data/METHODOLOGY.md`](data/METHODOLOGY.md) for the full
account, including where the calibration missed its target and why.

## The two layers

**[gtm-metrics-explorer](gtm-metrics-explorer/)** — a workflow. Answers
seven known, descriptive questions (pipeline health, win rate, MRR trend,
ARR waterfall, retention, top accounts, ticket mix) through a fixed,
predetermined sequence of hand-authored SQL queries, run for real through
the shared MCP server's `run_query` tool. No model involved, no API key
needed — every query is authored directly in `explorer.py`, not generated
or picked by Claude.

**[gtm-intelligence-investigator](gtm-intelligence-investigator/)** — an
agent, in two deliberately different shapes. A fully open-ended
`investigate()` loop hands Claude the same shared tools and lets it
decide, step by step, what to query next — a real agentic tool-use loop
against a real Anthropic API key, not a scripted demo (see
[`EXAMPLE_INVESTIGATION.md`](gtm-intelligence-investigator/EXAMPLE_INVESTIGATION.md)
for an actual, unedited run: the agent correctly traced a synthetic
revenue drop back to a specific planted incident — a March 2026 billing
dispute wave concentrated in Mid-Market SMS accounts — using nothing but
its own tool calls). But the project's nine locked-in demo questions
each run through their own **staged playbook** instead: a fixed,
analyst-defined sequence of deterministic code stages with Claude
narrowed to one bounded final synthesis step. That split exists because
building all nine with the open-ended loop first is what surfaced a real
mistake (summing incompatible usage units across channels and misreading
the result as a price change) worth fixing once, in code, rather than
re-risking on every run — see the Investigator's own README for the full
reasoning and `data/METHODOLOGY.md` for the bug itself.

Both layers are two clients of one shared MCP server —
[`mcp_server/analytics_server.py`](mcp_server/analytics_server.py) — so
the difference between them is never the data access layer, only whether
the caller follows a path decided in code or one decided by a model.

## Repo layout

```
metric-sleuth/
├── data/
│   ├── metric_sleuth.db       — SQLite: 1,400 opportunities, 292 accounts,
│   │                             15,790 usage rows, 436 tickets
│   └── METHODOLOGY.md         — the company, every table, every
│                                 calibration source, every verified number
├── mcp_server/
│   ├── analytics_server.py    — the shared MCP server (10 tools, 1
│   │                             resource, 1 prompt)
│   ├── generate_dataset.py    — pipeline + accounts + channel pricing
│   └── generate_usage_and_tickets.py  — usage, tickets, the planted incident
├── gtm-metrics-explorer/
│   ├── explorer.py            — the workflow layer
│   └── README.md
├── gtm-intelligence-investigator/
│   ├── investigator.py        — open-ended agent loop + dispatch to
│   │                             the 9 staged playbooks below
│   ├── playbook_lib.py        — shared helpers across playbooks
│   ├── retention_drop_playbook.py, q2_*.py … q9_*.py  — one staged,
│   │                             analyst-defined playbook per question
│   ├── EXAMPLE_INVESTIGATION.md  — a real, verified open-ended-loop run
│   └── README.md
├── tests/                     — 52 tests: dataset integrity, tool
│                                 correctness, real MCP protocol exchange
└── .github/workflows/ci.yml   — runs the test suite on every push
```

## Quick start

```bash
pip install -r requirements.txt

# Layer 1 — no API key needed
cd gtm-metrics-explorer && python3 explorer.py

# Layer 2 — needs your own Anthropic API key, set in your shell, never in a file
export ANTHROPIC_API_KEY=sk-ant-...
cd gtm-intelligence-investigator && python3 investigator.py

# Tests
python3 -m pytest tests/ -v
```

## Why this exists

Built as a hands-on project to learn the Model Context Protocol and
agentic tool use from the ground up — real MCP client/server protocol
exchanges throughout (nothing mocked), a dataset built and calibrated
against cited industry benchmarks rather than invented numbers, and an
agent held to actually citing evidence for its conclusions rather than
asserting them.
