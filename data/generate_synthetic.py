"""Generate synthetic source-system extracts for a fictional SME IT services
and telecoms provider ("Northbridge Comms Ltd").

Each source deliberately uses its own naming, ID formats, date formats and
quirks, the way real CRM, billing, service desk and network systems do. The
ingestion layer has to reconcile them.

All data is fictional. Run:  python data/generate_synthetic.py
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 42
N_CUSTOMERS = 120
RAW = Path(__file__).resolve().parent / "raw"

SECTORS = ["Manufacturing", "Healthcare", "Education", "Retail", "Logistics",
           "Professional Services", "Public Sector", "Hospitality"]
PRODUCTS = {
    "Hosted VoIP": (180, 900),
    "Leased Line": (350, 1400),
    "Cloud Hosting": (250, 2200),
    "Managed IT Support": (400, 3000),
    "Microsoft 365 Licences": (90, 800),
    "Data Centre Colocation": (600, 2600),
}
ACCOUNT_MANAGERS = ["A. Patel", "J. Morris", "S. Khan", "L. Turner"]
TICKET_CATEGORIES = ["Connectivity", "VoIP", "Email", "Hardware", "Hosting", "Billing query", "Security"]
TOWNS = ["Middlesbrough", "Stockton", "Darlington", "Hartlepool", "Redcar", "Durham", "York", "Hull"]
NAME_A = ["Tees", "North", "River", "Iron", "Harbour", "Cleveland", "Moor", "Brunel", "Dale", "Coast"]
NAME_B = ["Engineering", "Clinics", "Academy", "Foods", "Freight", "Partners", "Council Services",
          "Hotels", "Fabrication", "Labs", "Logistics", "Dental"]


def _company_names(rng: random.Random, n: int) -> list[str]:
    names: set[str] = set()
    while len(names) < n:
        names.add(f"{rng.choice(NAME_A)} {rng.choice(NAME_B)} {rng.choice(['Ltd', 'LLP', 'Group', 'plc'])}")
    return sorted(names)


def generate(out_dir: Path = RAW, seed: int = SEED) -> None:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date(2026, 9, 30)

    # ---- CRM export: IDs like "C-0007", UK date format, free-text sector casing
    customers = []
    for i, name in enumerate(_company_names(rng, N_CUSTOMERS), start=1):
        signup = today - timedelta(days=rng.randint(60, 2200))
        sector = rng.choice(SECTORS)
        customers.append({
            "Account ID": f"C-{i:04d}",
            "Company": name,
            "Sector": sector.upper() if rng.random() < 0.2 else sector,
            "Town": rng.choice(TOWNS),
            "Primary Contact Email": f"contact{i}@{name.split()[0].lower()}{i}.example.co.uk",
            "Account Manager": rng.choice(ACCOUNT_MANAGERS),
            "Customer Since": signup.strftime("%d/%m/%Y"),
            "Seats": rng.randint(5, 400),
        })
    with open(out_dir / "crm_accounts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(customers[0]))
        w.writeheader()
        w.writerows(customers)

    # Hidden "health" per customer drives correlated behaviour across systems
    health = {c["Account ID"]: rng.random() for c in customers}

    # ---- Billing export: IDs like "CUST0007", ISO dates, amounts as strings with £
    invoices = []
    inv_no = 10000
    for c in customers:
        cid = c["Account ID"]
        num = int(cid.split("-")[1])
        prods = rng.sample(list(PRODUCTS), k=rng.randint(1, 3))
        for m in range(12):
            month_start = date(today.year, today.month, 1) - timedelta(days=30 * (11 - m))
            for p in prods:
                lo, hi = PRODUCTS[p]
                amount = round(rng.uniform(lo, hi), 2)
                issue = month_start.replace(day=1)
                late = health[cid] < 0.25 and rng.random() < 0.5
                paid = None if (late and m >= 10) else issue + timedelta(days=rng.randint(35, 90) if late else rng.randint(5, 28))
                inv_no += 1
                invoices.append({
                    "invoice_no": f"INV{inv_no}",
                    "cust_ref": f"CUST{num:04d}",
                    "product_line": p,
                    "net_amount": f"£{amount:,.2f}",
                    "issued": issue.isoformat(),
                    "paid_on": paid.isoformat() if paid and paid <= today else "",
                })
    # Real exports are never clean: an invoice for an account missing from the CRM,
    # and an amount keyed in with a typo.
    invoices.append({**invoices[0], "invoice_no": "INV99001", "cust_ref": "CUST0999"})
    invoices.append({**invoices[1], "invoice_no": "INV99002", "net_amount": "£1,2O4.00"})
    with open(out_dir / "billing_invoices.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(invoices[0]))
        w.writeheader()
        w.writerows(invoices)

    # ---- Service desk: JSON, IDs as integers, timestamps with time, priority P1–P4
    tickets = []
    tid = 5000
    sla_hours = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}
    for c in customers:
        cid = c["Account ID"]
        n = rng.randint(0, 6) + (rng.randint(4, 12) if health[cid] < 0.3 else 0)
        for _ in range(n):
            tid += 1
            opened = datetime.combine(today, datetime.min.time()) - timedelta(
                days=rng.randint(0, 180), hours=rng.randint(0, 23))
            pr = rng.choices(["P1", "P2", "P3", "P4"], weights=[1, 3, 6, 4])[0]
            breach = rng.random() < (0.45 if health[cid] < 0.3 else 0.1)
            dur = sla_hours[pr] * (rng.uniform(1.1, 3) if breach else rng.uniform(0.1, 0.95))
            resolved = opened + timedelta(hours=dur)
            cat = rng.choice(TICKET_CATEGORIES)
            tickets.append({
                "ticket_id": tid,
                "client": int(cid.split("-")[1]),
                "priority": pr,
                "category": cat,
                "summary": f"{cat} issue reported by {c['Primary Contact Email']}",
                "opened_at": opened.strftime("%Y-%m-%dT%H:%M:%S"),
                "resolved_at": resolved.strftime("%Y-%m-%dT%H:%M:%S") if rng.random() > 0.08 else None,
                "csat": None if rng.random() < 0.4 else (rng.randint(1, 3) if health[cid] < 0.3 else rng.randint(3, 5)),
            })
    tickets.append({**tickets[0], "ticket_id": 99001, "client": "unknown"})
    (out_dir / "servicedesk_tickets.json").write_text(json.dumps(tickets, indent=1), encoding="utf-8")

    # ---- Network monitoring: monthly usage per site, account as "NB/0007", month as "2026-03"
    usage = []
    for c in customers:
        cid = c["Account ID"]
        num = int(cid.split("-")[1])
        base_min = rng.randint(500, 20000)
        for m in range(12):
            month = (date(today.year, today.month, 1) - timedelta(days=30 * (11 - m))).strftime("%Y-%m")
            trend = 1 - (0.05 * m if health[cid] < 0.25 else -0.01 * m)
            usage.append({
                "acct": f"NB/{num:04d}",
                "period": month,
                "voice_minutes": max(0, int(base_min * trend * rng.uniform(0.85, 1.15))),
                "data_gb": round(rng.uniform(20, 900) * max(trend, 0.2), 1),
                "outage_minutes": rng.randint(0, 240) if health[cid] < 0.3 else rng.randint(0, 30),
            })
    with open(out_dir / "network_usage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(usage[0]))
        w.writeheader()
        w.writerows(usage)

    print(f"Wrote {len(customers)} accounts, {len(invoices)} invoices, "
          f"{len(tickets)} tickets, {len(usage)} usage rows to {out_dir}")


if __name__ == "__main__":
    generate()
