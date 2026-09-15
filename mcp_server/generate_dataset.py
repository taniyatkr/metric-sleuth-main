"""
metric-sleuth — Wavemark dataset generator (pipeline + accounts + usage)

Wavemark is a FICTIONAL company invented for this project. It is modeled on
the real, publicly-known business shape of usage-based CPaaS providers
(companies that sell SMS/voice/email/verification APIs to other businesses,
billed by consumption rather than by seat) — it is not any specific real
company, and none of this data describes a real business.

This script generates three tables:

  opportunities   — the B2B sales pipeline that produces new accounts.
  accounts        — the customers created by won opportunities.
  channel_rates   — list price per unit for each API channel (reference table).

A second script, generate_usage_and_tickets.py, reads accounts back out of
the database and generates monthly_usage (realized, consumption-based
revenue) and support_tickets (a second, correlated signal). Usage is
generated separately because it needs to simulate month-by-month behavior
per account, which is a distinct piece of logic from the pipeline itself.

CALIBRATION SOURCES (cited plainly, not invented):

  - Sales cycle length by deal size, and overall B2B win rate (~20-21%):
    Gradient.works, "2025 B2B Sales Performance Benchmarks"
    https://www.gradient.works/blog/2025-b2b-sales-performance-benchmarks

  - Segment ACV bands (SMB / Mid-Market / Enterprise) and GRR/NRR ranges
    by segment: Bessemer Venture Partners, "State of the Cloud" (as
    summarized by SubJolt's 2026 NRR/GRR benchmarks guide)
    https://www.subjolt.com/guides/nrr-grr-benchmarks/

  - Usage-based vs. seat-based NRR (108% vs. 98%): Benchmarkit, "2026 SaaS
    and AI-Native Metrics" report, as summarized by SubJolt (above).

  Where sources disagree (m3ter puts usage-based NRR as high as 115-130%+,
  noticeably above Benchmarkit's 108%), we calibrate to the more
  conservative, more widely-corroborated Benchmarkit figure and say so —
  see data/METHODOLOGY.md for the full discussion, including what we
  actually measured after generating the data.

  Segment-level WIN RATE (as opposed to overall win rate and cycle length
  by deal size, which are both sourced above) is NOT broken out in either
  source. The SMB/Mid-Market/Enterprise split used below is our own
  reasonable assumption, tuned so the blended rate lands near the sourced
  ~20-21% overall figure — stated plainly as an assumption, not a citation.
"""

import random
import sqlite3
from datetime import date, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta

DB_PATH = "data/metric_sleuth.db"
random.seed(42)

FOUNDING_MONTH = date(2023, 1, 1)   # Wavemark's first month of pipeline activity
AS_OF_MONTH = date(2026, 9, 1)      # reference "current" month for the whole dataset

STAGES_ORDER = ["Prospecting", "Qualification", "Demo", "Proposal", "Negotiation"]

SEGMENTS = ["SMB", "Mid-Market", "Enterprise"]
SEGMENT_MIX = [0.60, 0.32, 0.08]                 # share of opportunities created
SEGMENT_WIN_RATE = {"SMB": 0.25, "Mid-Market": 0.18, "Enterprise": 0.12}
# blended check: 0.60*0.25 + 0.32*0.18 + 0.08*0.12 = 0.2172 -> ~21.7%, matches
# the ~20-21% sourced overall figure closely. These are win rate AS A SHARE
# OF OPPORTUNITIES THAT REACH QUALIFICATION (15% of all opportunities never
# do) -- divided below by that 0.85 qualification rate so the rate actually
# observed, over ALL created opportunities, lands on the target above.
QUALIFICATION_RATE = 0.85
SEGMENT_WIN_RATE = {k: v / QUALIFICATION_RATE for k, v in SEGMENT_WIN_RATE.items()}

SEGMENT_CYCLE_DAYS = {                            # (min, max) creation->close, in days
    "SMB": (14, 30),
    "Mid-Market": (30, 90),
    "Enterprise": (90, 210),                      # source says "90-180+", we allow some tail
}

SEGMENT_ACV_RANGE = {                             # qualified_amount sampling range, in USD
    "SMB": (3_000, 15_000),
    "Mid-Market": (15_000, 100_000),
    "Enterprise": (100_000, 450_000),
}

SOURCES = ["Outbound", "Inbound", "Partner"]
SOURCE_WEIGHTS = [0.45, 0.40, 0.15]

REPS = [
    "Priya Nandan", "Marcus Webb", "Elena Suárez", "Tom Ferraro", "Dana Kwiat",
    "Isaac Berhane", "Nadia Okafor", "Chris Lindqvist", "Sara Vandenberg", "Leo Farah",
]

CHANNELS = ["sms", "voice", "email", "verification"]
CHANNEL_UNIT = {"sms": "message", "voice": "minute", "email": "email", "verification": "check"}
CHANNEL_LIST_PRICE = {"sms": 0.0075, "voice": 0.013, "email": 0.0010, "verification": 0.0500}
PRIMARY_CHANNEL_WEIGHTS = {"sms": 0.50, "voice": 0.20, "email": 0.20, "verification": 0.10}

INDUSTRIES = [
    "Logistics & Delivery", "Retail & E-commerce", "Healthcare", "Fintech",
    "Insurance", "Real Estate", "Field Services", "Foodtech", "Marketplace",
    "Education",
]

# ---- synthetic company name generation ----------------------------------

_NAME_PREFIX = [
    "North", "Cedar", "Bright", "River", "Summit", "Clear", "Vale", "Harbor",
    "Granite", "Amber", "Blue", "Iron", "Silver", "Willow", "Copper", "Maple",
    "Fern", "Stone", "Coral", "Pine", "Frost", "Lark", "Nova", "Terra",
]
_NAME_SUFFIX = [
    "line", "field", "point", "wood", "gate", "ridge", "bridge", "stead",
    "brook", "haven", "peak", "grove", "well", "port", "mark", "reach",
]
_NAME_INDUSTRY_WORD = {
    "Logistics & Delivery": ["Logistics", "Freight", "Dispatch", "Routes"],
    "Retail & E-commerce": ["Retail", "Commerce", "Goods", "Mercantile"],
    "Healthcare": ["Health", "Clinical", "Care", "Medical"],
    "Fintech": ["Financial", "Capital", "Payments", "Ledger"],
    "Insurance": ["Assurance", "Insurance", "Underwriting"],
    "Real Estate": ["Realty", "Properties", "Estates"],
    "Field Services": ["Field Services", "Trades", "Services"],
    "Foodtech": ["Foods", "Kitchen", "Provisions"],
    "Marketplace": ["Marketplace", "Exchange", "Trading"],
    "Education": ["Learning", "Academy", "Education"],
}


def make_company_name(industry: str) -> str:
    base = random.choice(_NAME_PREFIX) + random.choice(_NAME_SUFFIX)
    word = random.choice(_NAME_INDUSTRY_WORD[industry])
    return f"{base} {word}"


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def month_add(d: date, n: int) -> date:
    return d + relativedelta(months=n)


def sample_segment() -> str:
    return random.choices(SEGMENTS, weights=SEGMENT_MIX)[0]


def gen_opportunities(n: int) -> pd.DataFrame:
    """
    Generates the sales pipeline. Every opportunity is either resolved
    (Closed Won / Closed Lost) or, for a slice of recently-created ones,
    genuinely still open as of AS_OF_MONTH — so pipeline-health questions
    ("what's currently in flight") have real data to answer, not just
    closed history.
    """
    total_days = (AS_OF_MONTH - FOUNDING_MONTH).days
    rows = []

    for i in range(n):
        segment = sample_segment()
        source = random.choices(SOURCES, weights=SOURCE_WEIGHTS)[0]
        rep = random.choice(REPS)

        created_offset = random.randint(0, total_days)
        created_date = add_days(FOUNDING_MONTH, created_offset)

        cycle_min, cycle_max = SEGMENT_CYCLE_DAYS[segment]
        cycle_days = random.randint(cycle_min, cycle_max)
        planned_close = add_days(created_date, cycle_days)

        qualified_amount = round(random.uniform(*SEGMENT_ACV_RANGE[segment]), 2)

        # a fixed share of opportunities die before ever reaching "Qualification".
        reaches_qualified = random.random() < QUALIFICATION_RATE
        qualified_date = None
        if reaches_qualified:
            # qualified sometime in the first 20-40% of the cycle
            q_offset = int(cycle_days * random.uniform(0.20, 0.40))
            qualified_date = add_days(created_date, q_offset)

        if planned_close > AS_OF_MONTH:
            # still open as of the reference month
            stage = random.choice(STAGES_ORDER) if reaches_qualified else "Prospecting"
            # can't have progressed past Qualification if it never qualified
            if not reaches_qualified:
                stage = "Prospecting"
            rows.append({
                "opportunity_id": i + 1,
                "segment": segment,
                "source": source,
                "rep": rep,
                "created_date": created_date.isoformat(),
                "qualified_date": qualified_date.isoformat() if qualified_date else None,
                "qualified_amount": qualified_amount if reaches_qualified else None,
                "stage": stage,
                "close_date": None,
                "closed_amount": None,
                "is_won": None,
            })
            continue

        is_won = reaches_qualified and (random.random() < SEGMENT_WIN_RATE[segment])

        if is_won:
            stage = "Closed Won"
            # deal size often moves between qualification and close
            slip = random.uniform(0.70, 1.30)
            closed_amount = round(qualified_amount * slip, 2)
        else:
            stage = "Closed Lost"
            closed_amount = None

        rows.append({
            "opportunity_id": i + 1,
            "segment": segment,
            "source": source,
            "rep": rep,
            "created_date": created_date.isoformat(),
            "qualified_date": qualified_date.isoformat() if qualified_date else None,
            "qualified_amount": qualified_amount if reaches_qualified else None,
            "stage": stage,
            "close_date": planned_close.isoformat(),
            "closed_amount": closed_amount,
            "is_won": int(is_won),
        })

    return pd.DataFrame(rows)


def gen_accounts(opportunities: pd.DataFrame) -> pd.DataFrame:
    won = opportunities[opportunities["is_won"] == 1].copy()
    won = won.sort_values("close_date").reset_index(drop=True)

    rows = []
    for i, row in enumerate(won.itertuples(index=False), start=1):
        industry = random.choice(INDUSTRIES)
        primary_channel = random.choices(
            list(PRIMARY_CHANNEL_WEIGHTS), weights=list(PRIMARY_CHANNEL_WEIGHTS.values())
        )[0]
        rows.append({
            "account_id": i,
            "opportunity_id": row.opportunity_id,
            "company_name": make_company_name(industry),
            "segment": row.segment,
            "industry": industry,
            "signup_date": row.close_date,
            "primary_channel": primary_channel,
            "starting_mrr": round(row.closed_amount / 12, 2),
        })
    return pd.DataFrame(rows)


def gen_channel_rates() -> pd.DataFrame:
    return pd.DataFrame([
        {"channel": c, "unit_name": CHANNEL_UNIT[c], "list_price_usd": CHANNEL_LIST_PRICE[c]}
        for c in CHANNELS
    ])


def main():
    opportunities = gen_opportunities(1400)
    accounts = gen_accounts(opportunities)
    channel_rates = gen_channel_rates()

    conn = sqlite3.connect(DB_PATH)
    opportunities.to_sql("opportunities", conn, index=False, if_exists="replace")
    accounts.to_sql("accounts", conn, index=False, if_exists="replace")
    channel_rates.to_sql("channel_rates", conn, index=False, if_exists="replace")

    conn.execute("CREATE INDEX idx_opp_segment ON opportunities(segment)")
    conn.execute("CREATE INDEX idx_opp_close ON opportunities(close_date)")
    conn.execute("CREATE INDEX idx_accounts_signup ON accounts(signup_date)")
    conn.commit()

    n_won = int((opportunities["is_won"] == 1).sum())
    n_lost = int((opportunities["is_won"] == 0).sum())
    n_open = int(opportunities["is_won"].isna().sum())
    print(f"opportunities: {len(opportunities)} rows "
          f"({n_won} won, {n_lost} lost, {n_open} still open)")
    print(f"overall win rate (of resolved): {n_won / (n_won + n_lost):.1%}")
    print(f"accounts: {len(accounts)} rows")
    print(f"channel_rates: {len(channel_rates)} rows")
    conn.close()


if __name__ == "__main__":
    main()
