"""
GTM Intelligence Investigator — an AGENT, not a workflow.

This is Layer 2 of metric-sleuth. Where gtm-metrics-explorer follows a
fixed, predetermined sequence of SQL queries per question,
investigate(question) hands Claude the shared MCP tools and lets it
decide, step by step, which to call and in what order -- because for a
genuinely novel question, the right sequence of queries isn't knowable
in advance. That's the actual definition of "agent" this project is
built around: not a bigger model, a dynamic path instead of a fixed one.
The loop below is the standard agentic tool-use pattern: send the
question and the tool list to Claude, and if it responds with
stop_reason "tool_use", actually execute that tool call against the real
MCP server, feed the result back, and ask again -- repeating until
Claude has enough evidence to stop on its own ("end_turn"), or a hard
iteration cap is hit. investigate() is that raw capability, still fully
present below and callable on any question.

TWO SHAPES OF INVESTIGATION IN THIS FILE -- read this before the code:

For the 9 specific questions this project set out to answer (see
DEMO_QUESTIONS), main() does NOT route through the open-ended loop above.
It calls one of the 9 staged playbook files instead (retention_drop_
playbook.py, q2_mid_market_underperformance.py, ... q9_source_rep_
quality.py) -- each an analyst-defined, fixed sequence of deterministic
code stages, with Claude's role narrowed to a final synthesis stage that
gets no tools and can't query anything itself, only reason over the
evidence dict the code stages already assembled. Why: building all 9
with the fully open-ended loop first showed the same failure mode twice
-- an early version of the loop (and, independently, an early version of
one playbook's own stage) summed raw usage `units` across incompatible
channel types and read a pure channel-mix shift as a price increase that
never happened (see data/METHODOLOGY.md). A dynamic agent re-derives its
whole approach every run, including whatever mistake is easiest to make
again; a staged playbook fixes the correct approach once, in code, after
an analyst has actually caught and corrected that mistake. For a *known*
question asked repeatedly, that's a strictly better trade -- reproducible
numbers, thresholds a reviewer can see and challenge, and a Claude call
that's doing the one thing it's actually good for here (writing up a
conclusion from evidence) instead of the one thing that went wrong twice
(deciding, from scratch, what "usage" should mean).

That leaves a real question about which parts of investigate()'s open-
ended agentic loop still earn a place here: it remains the only
Investigator entry point for a question that ISN'T one of the 9 --
type any other question at the CLI and it runs the full dynamic loop,
tool list and all, exactly as this file always has. So the two layers of
this project make one comparison (workflow vs. agent) and this file
alone makes a second, one level in: a fully dynamic agent is the right
tool for a genuinely novel question, and a staged, analyst-defined
pipeline with a narrowly-bounded model step is the right tool once you
know which question you're going to keep asking. Same underlying
capability (an LLM reasoning over real tool/query results), two very
different amounts of freedom, chosen deliberately for two different
situations -- not because one is more "agentic" than the other, but
because they answer different needs.

Requires an Anthropic API key in the ANTHROPIC_API_KEY environment
variable (never hard-code it here, and never paste it into chat with an
assistant either -- set it in your own shell/terminal). This is the only
layer of metric-sleuth that costs API money to run; gtm-metrics-explorer
needs no key at all. Within this layer, the 9 staged playbooks each need
a key ONLY for their one synthesis stage -- every diagnostic stage before
it runs on plain deterministic code and needs no key at all.
"""

import asyncio
import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "analytics_server.py")
SERVER_PARAMS = StdioServerParameters(command="python3", args=[SERVER_PATH])

MODEL = "claude-sonnet-5"
MAX_ITERATIONS = 12  # guardrail: hard stop so a bad loop can't run away -- raised
                     # from 8 after a live run showed it correctly narrowing all
                     # the way to the root cause but running out of room to
                     # synthesize a final answer (see data/METHODOLOGY.md note)

SYSTEM_PROMPT = """You are the GTM Intelligence Investigator for Wavemark, \
a B2B communications API company. You have MCP tools that query Wavemark's \
real sales pipeline, usage revenue, retention, and support-ticket data.

Investigate the question by calling tools -- start with the purpose-built \
ones (pipeline_summary, retention, retention_decomposition, top_accounts, \
support_ticket_summary, get_account_detail), and fall back to run_query for \
anything they don't cover. Narrow from a blended/aggregate view down to the \
specific segment, channel, account, or time window driving it, the same \
way a real analyst would.

Do not stop at a plausible-sounding guess. Every claim in your final \
answer must be backed by a specific number or result you actually \
retrieved through a tool call in this conversation -- cite the tool and \
the figure (e.g. "retention_decomposition showed 3 Mid-Market accounts \
contracting by $5k+ each"). If you cannot find clear evidence for a \
cause, say so plainly rather than speculating."""


def mcp_tools_to_anthropic_format(mcp_tools) -> list[dict]:
    """Translates MCP's tool listing (name/description/inputSchema) into
    the shape the Anthropic API expects (name/description/input_schema).
    This is done live from list_tools() rather than hand-duplicating the
    tool definitions here, so the two can never drift out of sync."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


async def investigate(question: str, verbose: bool = True) -> str:
    """Runs the full agentic loop for one question and returns Claude's
    final answer. Prints every tool call and result as it happens when
    verbose=True, so the investigation's evidence trail is visible, not
    just its conclusion."""
    anthropic_client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = mcp_tools_to_anthropic_format(mcp_tools)

            messages = [{"role": "user", "content": question}]

            for iteration in range(1, MAX_ITERATIONS + 1):
                response = anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    final_text = "".join(
                        block.text for block in response.content if block.type == "text"
                    )
                    if verbose:
                        print(f"\n[investigation complete after {iteration} iteration(s)]\n")
                    return final_text

                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    if verbose:
                        print(f"  [{iteration}] -> {block.name}({block.input})")
                    result = await session.call_tool(block.name, block.input)
                    result_text = result.content[0].text
                    if verbose:
                        preview = result_text[:200].replace("\n", " ")
                        print(f"        <- {preview}{'...' if len(result_text) > 200 else ''}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
                messages.append({"role": "user", "content": tool_results})

            return (
                f"[stopped: hit the {MAX_ITERATIONS}-iteration guardrail without "
                "Claude reaching a final answer -- the loop is working as designed "
                "(it can't run away), but this question needs a higher cap or a "
                "narrower starting point.]"
            )


# The nine Investigator questions locked in with the plan -- flagship
# question first, since it's the one with a real, verifiable answer. Each
# maps to its own staged playbook module (see the module docstring above
# for why these nine run staged rather than through investigate()).
DEMO_QUESTIONS = {
    "1": "Why did Mid-Market retention/NRR drop this year?",
    "2": "What's driving the Mid-Market segment's underperformance?",
    "3": "Why are we seeing a spike in billing complaints this spring?",
    "4": "What's driving our overall NRR this quarter (ending August 2026)?",
    "5": "Which accounts are at risk of churning right now?",
    "6": "What typically precedes a cancellation for our customers?",
    "7": "Is account 199 showing warning signs?",
    "8": "Why is our win rate lower this quarter than last?",
    "9": "Which source or rep is producing the best-quality pipeline?",
}


def _run_playbook_for(key: str):
    """Imports and runs the staged playbook module for one of the nine
    demo questions. Imported lazily, inside the function, rather than at
    module load -- these files pull in sqlite3/anthropic on their own and
    there's no reason to pay that cost for someone just running
    investigate() on a freeform question."""
    if key == "1":
        from retention_drop_playbook import run_flagship_playbook
        return run_flagship_playbook()
    if key == "2":
        from q2_mid_market_underperformance import run_underperformance_playbook
        return run_underperformance_playbook()
    if key == "3":
        from q3_billing_complaint_spike import run_billing_spike_playbook
        return run_billing_spike_playbook()
    if key == "4":
        from q4_overall_nrr import run_overall_nrr_playbook
        return run_overall_nrr_playbook()
    if key == "5":
        from q5_churn_risk_scoring import run_churn_risk_scoring
        return run_churn_risk_scoring()
    if key == "6":
        from q6_cancellation_precursors import run_cancellation_precursor_analysis
        return run_cancellation_precursor_analysis()
    if key == "7":
        from q7_account_watch_check import run_account_watch_check
        return run_account_watch_check()
    if key == "8":
        from q8_win_rate_drop import run_win_rate_drop_playbook
        return run_win_rate_drop_playbook()
    if key == "9":
        from q9_source_rep_quality import run_source_rep_quality_ranking
        return run_source_rep_quality_ranking()
    raise ValueError(f"no playbook registered for key {key!r}")


async def main():
    if len(sys.argv) > 1 and sys.argv[1] in DEMO_QUESTIONS:
        # One of the nine locked-in questions -- run its staged playbook,
        # not the open-ended loop. Each playbook module prints its own
        # stage-by-stage trace, so nothing further to print here.
        key = sys.argv[1]
        print(f"Question: {DEMO_QUESTIONS[key]}\n")
        print(f"(running the staged playbook for question {key} -- see "
              f"the module docstring in this file for why these nine "
              f"questions don't go through investigate()'s open-ended loop)\n")
        _run_playbook_for(key)
        return

    # Anything else -- a freeform question typed at the CLI -- runs the
    # real open-ended agentic loop. This is the only path in the whole
    # repo where the sequence of tool calls is decided by the model, not
    # by an analyst ahead of time.
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
    else:
        question = DEMO_QUESTIONS["1"]  # flagship question by default

    print(f"Question: {question}\n")
    answer = await investigate(question)
    print(f"\n{answer}")


if __name__ == "__main__":
    # A key is only required for investigate()'s open-ended loop, where
    # EVERY step is a Claude call. Each of the 9 staged playbooks needs a
    # key only for its own final synthesis stage, and already handles a
    # missing key gracefully by skipping just that stage (see
    # playbook_lib.claude_synthesize) -- so don't block a playbook run
    # over it here.
    running_a_playbook = len(sys.argv) > 1 and sys.argv[1] in DEMO_QUESTIONS
    if not running_a_playbook and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY isn't set. Set it in your shell before running this "
            "(e.g. export ANTHROPIC_API_KEY=sk-ant-...) -- never hard-code a key in "
            "this file or paste it into chat."
        )
        sys.exit(1)
    asyncio.run(main())
