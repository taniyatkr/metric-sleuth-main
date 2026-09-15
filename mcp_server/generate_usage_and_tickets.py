"""
metric-sleuth — Wavemark usage + support tickets generator

Reads the `accounts` table (created by generate_dataset.py) and generates:

  monthly_usage    — realized, consumption-based revenue per account, per
                     channel, per month. Because usage genuinely rises and
                     falls month to month (unlike a flat subscription rate),
                     expansion and contraction are real, queryable signals
                     here — not something the source data is silent on.

  support_tickets  — a second, fully SYNTHETIC data source (Wavemark has no
                     real support data any more than it has a real anything
                     else — the whole company is invented). Ticket volume
                     and category are deliberately biased to climb in the
                     months right before an account's usage collapses to
                     zero, mirroring a well-known real pattern (accounts in
                     trouble file more, and worse, tickets before they
                     leave). This gives gtm-intelligence-investigator a
                     genuine join-able signal to discover on its own,
                     rather than a hand-fed conclusion.

PLANTED INCIDENT (the Investigator's actual mystery to solve)

In March 2026, Wavemark raised SMS pricing specifically for Mid-Market
accounts (see INCIDENT_* constants below). Every account in that segment
and channel that was already a customer before the hike takes an
immediate usage hit that month, a lingering decline for a few months
after, and a materially elevated chance of fully cancelling within 1-3
months -- including accounts whose trajectory otherwise had them stable
or even expanding, which is what makes this a genuine "why did this
happen" story rather than more of the ambient churn pattern above. The
same accounts also get a wave of billing_dispute tickets dated
March-April 2026, skewed toward high/urgent priority.

Nothing in the schema flags this anywhere -- no incident column, no
account tag. It's discoverable only by noticing that revenue contraction
and billing_dispute tickets both concentrate in Mid-Market/sms accounts
starting March 2026, the same way the general ticket-churn correlation
above has to be found by querying, not read off a flag. The ground truth
is documented plainly in data/METHODOLOGY.md as an answer key, exactly
like the ticket-churn correlation was documented as synthetic there.

TRAJECTORY MODEL

Each account is assigned one of four monthly-revenue trajectories, with
segment-specific probabilities chosen so the blended result lands near the
retention benchmarks documented in data/METHODOLOGY.md (Bessemer segment
GRR bands; Benchmarkit's 108% usage-based NRR):

  expanding    revenue compounds up (~+1.5%/mo average)
  stable       flat, with noise
  contracting  revenue compounds down (~-3%/mo average) -- may or may not
               fully churn within the observation window
  churned      behaves like "stable" for a while, then drops sharply over
               a short window and goes to (and stays at) zero

These parameters are OUR OWN reasonable choices, not sourced figures --
GRR/NRR benchmarks tell us what the aggregate outcome should look like,
not the month-by-month mechanics that produce it. After generating the
data we verify the actual blended and per-segment GRR/NRR with real SQL
(see data/METHODOLOGY.md) rather than assuming the targets were hit.
"""

import random
import sqlite3
from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta

DB_PATH = "data/metric_sleuth.db"
AS_OF_MONTH = date(2026, 9, 1)
random.seed(99)

TRAJECTORY_MIX = {
    "SMB":         {"expanding": 0.20, "stable": 0.30, "contracting": 0.25, "churned": 0.25},
    "Mid-Market":  {"expanding": 0.30, "stable": 0.35, "contracting": 0.20, "churned": 0.15},
    "Enterprise":  {"expanding": 0.45, "stable": 0.40, "contracting": 0.10, "churned": 0.05},
}

CHURN_FLOOR_USD = 5.0        # below this, an account is treated as fully churned

CHANNEL_LIST_PRICE = {"sms": 0.0075, "voice": 0.013, "email": 0.0010, "verification": 0.0500}
SECONDARY_CHANNELS = list(CHANNEL_LIST_PRICE)

# --- planted incident: March 2026 SMS price hike for Mid-Market accounts ---
INCIDENT_MONTH = date(2026, 3, 1)
INCIDENT_SEGMENT = "Mid-Market"
INCIDENT_CHANNEL = "sms"                # affects accounts whose primary_channel is this
INCIDENT_IMMEDIATE_SHOCK = 0.78         # revenue multiplier in the incident month itself
INCIDENT_LINGERING_MONTHS = 3           # how many months the extra decay continues after
INCIDENT_LINGERING_DECAY = 0.95         # extra monthly multiplier during that window
INCIDENT_EXTRA_CHURN_PROB = 0.22        # chance a non-"churned"-trajectory affected account
                                         # fully cancels within 1-3 months of the hike anyway

TICKET_CATEGORIES = [
    "delivery_failures", "rate_limit", "integration_issue",
    "billing_dispute", "cancellation_request", "other",
]
TICKET_PRIORITIES = ["low", "medium", "high", "urgent"]


def month_add(d: date, n: int) -> date:
    return d + relativedelta(months=n)


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def pick_trajectory(segment: str) -> str:
    mix = TRAJECTORY_MIX[segment]
    return random.choices(list(mix), weights=list(mix.values()))[0]


def simulate_account_revenue(
    starting_mrr: float,
    n_months: int,
    trajectory: str,
    incident_idx: int | None = None,
    incident_forced_churn: bool = False,
):
    """
    Returns a list of length n_months of monthly total revenue (float),
    and the index (or None) of the first month where revenue hit the
    churn floor -- from that month on, revenue is 0 and stays 0.

    incident_idx: the tenure-relative month index (0-based, relative to
    this account's signup month) of the planted March-2026 incident, or
    None if this account isn't affected. incident_forced_churn: whether
    this account additionally cancels specifically because of the
    incident (rolled once, upstream, only for accounts whose base
    trajectory wasn't already "churned").
    """
    revenue = []
    current = max(starting_mrr, 50.0)
    churned_at = None

    if trajectory == "churned":
        decline_start = random.randint(max(1, n_months // 3), max(1, n_months - 2))
        decline_len = random.randint(2, 4)
    else:
        decline_start = None
        decline_len = None

    if incident_forced_churn:
        incident_decline_start = incident_idx + random.randint(1, 3)
        incident_decline_len = random.randint(2, 3)
    else:
        incident_decline_start = None
        incident_decline_len = None

    for m in range(n_months):
        if churned_at is not None:
            revenue.append(0.0)
            continue

        in_natural_decline = (
            trajectory == "churned" and decline_start <= m < decline_start + decline_len
        )
        in_incident_decline = (
            incident_forced_churn
            and incident_decline_start <= m < incident_decline_start + incident_decline_len
        )

        if in_natural_decline or in_incident_decline:
            growth = random.gauss(0.55, 0.08)        # sharp drop, whichever crisis it is
        elif trajectory == "expanding":
            growth = random.gauss(1.022, 0.015)
        elif trajectory == "stable":
            growth = random.gauss(1.000, 0.02)
        elif trajectory == "contracting":
            growth = random.gauss(0.97, 0.02)
        elif trajectory == "churned":
            growth = random.gauss(1.000, 0.02)       # looks normal outside its decline window
        else:
            raise ValueError(trajectory)

        current = max(0.0, current * growth)

        # the planted incident: an immediate hit in the incident month
        # itself, then a lingering extra decay for a few months after --
        # applied on top of whatever the base trajectory was already
        # doing, and it propagates forward since `current` carries into
        # the next month's growth.
        if incident_idx is not None:
            if m == incident_idx:
                current *= INCIDENT_IMMEDIATE_SHOCK
            elif incident_idx < m <= incident_idx + INCIDENT_LINGERING_MONTHS:
                current *= INCIDENT_LINGERING_DECAY

        revenue.append(round(current, 2))

        # an account cancels the month right after ITS decline window
        # ends (natural or incident-forced), regardless of whether decay
        # alone reached the floor -- the decline window IS the lead-up to
        # cancellation, this last low-usage month is real, then they're
        # gone.
        end_of_natural_decline = trajectory == "churned" and m == decline_start + decline_len - 1
        end_of_incident_decline = (
            incident_forced_churn and m == incident_decline_start + incident_decline_len - 1
        )
        if current < CHURN_FLOOR_USD or end_of_natural_decline or end_of_incident_decline:
            churned_at = m + 1

    return revenue, churned_at


def split_across_channels(total_revenue: float, primary_channel: str):
    """Splits one month's total revenue across channels: primary channel
    gets the lion's share, one or two secondary channels split the rest."""
    if total_revenue <= 0:
        return {}

    primary_share = random.uniform(0.55, 0.80)
    result = {primary_channel: total_revenue * primary_share}

    remaining = total_revenue - result[primary_channel]
    others = [c for c in SECONDARY_CHANNELS if c != primary_channel]
    random.shuffle(others)
    n_others = random.choice([1, 2])
    weights = [random.random() for _ in range(n_others)]
    wsum = sum(weights)
    for c, w in zip(others[:n_others], weights):
        result[c] = remaining * (w / wsum)

    return result


def gen_monthly_usage(accounts: pd.DataFrame):
    usage_rows = []
    churn_info = {}          # account_id -> churned_month (date) or None
    incident_affected = set()  # account_ids hit by the March-2026 SMS price hike

    for row in accounts.itertuples(index=False):
        signup = date.fromisoformat(row.signup_date)
        n_months = months_between(signup, AS_OF_MONTH) + 1
        if n_months <= 0:
            continue

        trajectory = pick_trajectory(row.segment)

        is_incident_candidate = (
            row.segment == INCIDENT_SEGMENT
            and row.primary_channel == INCIDENT_CHANNEL
            and months_between(signup, INCIDENT_MONTH) >= 0
        )
        incident_idx = months_between(signup, INCIDENT_MONTH) if is_incident_candidate else None
        incident_forced_churn = (
            is_incident_candidate
            and trajectory != "churned"
            and random.random() < INCIDENT_EXTRA_CHURN_PROB
        )
        if is_incident_candidate:
            incident_affected.add(row.account_id)

        revenue_series, churned_at = simulate_account_revenue(
            row.starting_mrr, n_months, trajectory, incident_idx, incident_forced_churn
        )
        churn_info[row.account_id] = (
            month_add(signup, churned_at) if churned_at is not None else None
        )

        for i, month_revenue in enumerate(revenue_series):
            if month_revenue <= 0:
                continue
            month = month_add(signup, i)
            split = split_across_channels(month_revenue, row.primary_channel)
            for channel, chan_revenue in split.items():
                price = CHANNEL_LIST_PRICE[channel]
                units = max(1, round(chan_revenue / price))
                usage_rows.append({
                    "account_id": row.account_id,
                    "month": month.strftime("%Y-%m"),
                    "channel": channel,
                    "units": units,
                    "revenue": round(units * price, 2),
                })

    return pd.DataFrame(usage_rows), churn_info, incident_affected


def gen_support_tickets(accounts: pd.DataFrame, churn_info: dict, incident_affected: set):
    rows = []
    ticket_id = 1

    for row in accounts.itertuples(index=False):
        signup = date.fromisoformat(row.signup_date)
        churned_month = churn_info.get(row.account_id)
        n_months = months_between(signup, AS_OF_MONTH) + 1
        if n_months <= 0:
            continue

        if churned_month is not None:
            # heavy ticket volume, clustered in the 3 months before churn
            n_tickets = random.choices([1, 2, 3, 4, 5, 6], weights=[10, 15, 20, 25, 20, 10])[0]
            window = min(months_between(signup, churned_month) + 1, 3)
        else:
            n_tickets = random.choices([0, 1, 2, 3], weights=[50, 30, 15, 5])[0]
            window = None

        for _ in range(n_tickets):
            if churned_month is not None:
                months_before = random.choices(range(window), weights=[10, 30, 60][:window])[0]
                offset = max(0, months_between(signup, churned_month) - months_before)
                category = random.choices(
                    TICKET_CATEGORIES, weights=[15, 10, 15, 20, 30, 10]
                )[0]
                priority = random.choices(TICKET_PRIORITIES, weights=[10, 20, 35, 35])[0]
            else:
                offset = random.randint(0, n_months - 1)
                category = random.choices(
                    TICKET_CATEGORIES, weights=[30, 25, 25, 10, 2, 8]
                )[0]
                priority = random.choices(TICKET_PRIORITIES, weights=[45, 35, 15, 5])[0]

            offset = max(0, min(offset, n_months - 1))
            opened = month_add(signup, offset).replace(day=random.randint(1, 28))
            resolved = random.random() < (0.55 if churned_month is not None else 0.85)

            rows.append({
                "ticket_id": ticket_id,
                "account_id": row.account_id,
                "opened_date": opened.isoformat(),
                "category": category,
                "priority": priority,
                "resolved": int(resolved),
            })
            ticket_id += 1

        # planted incident: affected accounts also file a wave of billing
        # complaints right when the price hike lands, on top of whatever
        # their ordinary ticket pattern already generated above.
        if row.account_id in incident_affected:
            n_incident_tickets = random.choices([1, 2, 3], weights=[30, 45, 25])[0]
            for _ in range(n_incident_tickets):
                opened = month_add(INCIDENT_MONTH, random.choice([0, 1])).replace(
                    day=random.randint(1, 28)
                )
                rows.append({
                    "ticket_id": ticket_id,
                    "account_id": row.account_id,
                    "opened_date": opened.isoformat(),
                    "category": "billing_dispute",
                    "priority": random.choices(
                        TICKET_PRIORITIES, weights=[5, 15, 40, 40]
                    )[0],
                    "resolved": int(random.random() < 0.5),
                })
                ticket_id += 1

    return pd.DataFrame(rows)


def main():
    conn = sqlite3.connect(DB_PATH)
    accounts = pd.read_sql_query(
        "SELECT account_id, segment, signup_date, primary_channel, starting_mrr FROM accounts",
        conn,
    )

    usage, churn_info, incident_affected = gen_monthly_usage(accounts)
    tickets = gen_support_tickets(accounts, churn_info, incident_affected)

    usage.to_sql("monthly_usage", conn, index=False, if_exists="replace")
    tickets.to_sql("support_tickets", conn, index=False, if_exists="replace")

    conn.execute("CREATE INDEX idx_usage_account ON monthly_usage(account_id)")
    conn.execute("CREATE INDEX idx_usage_month ON monthly_usage(month)")
    conn.execute("CREATE INDEX idx_tickets_account ON support_tickets(account_id)")
    conn.execute("CREATE INDEX idx_tickets_opened ON support_tickets(opened_date)")
    conn.commit()

    n_churned = sum(1 for v in churn_info.values() if v is not None)
    print(f"monthly_usage: {len(usage)} rows")
    print(f"support_tickets: {len(tickets)} rows")
    print(f"accounts fully churned by {AS_OF_MONTH.isoformat()}: {n_churned} / {len(accounts)}")
    print(f"accounts affected by the March 2026 SMS price hike: {len(incident_affected)}")
    conn.close()


if __name__ == "__main__":
    main()
