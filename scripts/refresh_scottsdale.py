"""Daily refresh of ktr-scottsdale/data.json from Windsor.ai.

Re-pulls the last 7 days every run (so late-posting purchases land), checks the
numbers, merges into the existing file, and only then writes. If any check
fails it exits non-zero without writing, so the dashboard keeps its last good
data and GitHub flags the failed run.

KTR Scottsdale rules (separate from every other KTR location):
- Purchases: GA4 property 422830474 ("Scottsdale - GA4"), sessions from a
  Google Ads campaign whose name contains "Scottsdale". Item-level rows.
- Spend/clicks: Google Ads account 440-633-6580 (all campaigns).
- Brand spend (kept in the data, not shown yet): search terms containing
  "ktr" or "kids that rip".
- Dashboard starts 2026-09-24.
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

API = "https://connectors.windsor.ai"
KEY = os.environ.get("WINDSOR_API_KEY", "").strip()
DATA = os.path.join(os.path.dirname(__file__), "..", "ktr-scottsdale", "data.json")
GA4_ACCOUNT = "422830474"
ADS_ACCOUNT = "440-633-6580"
CAMPAIGN_MARK = "scottsdale"
BRAND_MARKS = ("ktr", "kids that rip")
START = "2026-09-24"
WINDOW_DAYS = 7


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def pull(connector, account, fields, date_from, date_to):
    params = {"api_key": KEY, "date_from": date_from, "date_to": date_to,
              "fields": ",".join(fields), "select_accounts": account}
    url = f"{API}/{connector}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ktr-reports-refresh"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # never print the URL: it carries the key
        detail = e.read().decode(errors="replace")[:800].replace(KEY, "***")
        fail(f"Windsor request failed for {connector} ({', '.join(fields)}): HTTP {e.code}: {detail}")
    except Exception as e:
        fail(f"Windsor request failed for {connector}: {type(e).__name__}: {e}".replace(KEY, "***"))
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        fail(f"Unexpected Windsor response for {connector}")
    print(f"{connector} {fields}: {len(rows)} rows")
    return rows


def iso(v):
    s = str(v).replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        fail(f"Unexpected date value: {v!r}")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def num(v):
    return float(v or 0)


def is_ad(r):
    return CAMPAIGN_MARK in str(r.get("session_google_ads_campaign_name") or "").lower()


def kind(q):
    if not q or q == "(not set)":
        return "u"
    return "b" if any(m in q.lower() for m in BRAND_MARKS) else "o"


def main():
    if not KEY:
        fail("WINDSOR_API_KEY secret is missing")
    today = dt.datetime.now(ZoneInfo("America/Phoenix")).date()
    yesterday = today - dt.timedelta(days=1)
    start = max(yesterday - dt.timedelta(days=WINDOW_DAYS - 1), dt.date.fromisoformat(START))
    d_from, d_to = start.isoformat(), yesterday.isoformat()
    print(f"Window {d_from} .. {d_to}")

    # 1) Purchases, item level
    raw = pull("googleanalytics4", GA4_ACCOUNT,
               ["date", "transactionid", "item_name", "item_revenue", "items_purchased", "session_google_ads_campaign_name"],
               d_from, d_to)
    lines = [(iso(r["date"]), str(r.get("transactionid") or ""), str(r.get("item_name") or ""),
              int(num(r.get("items_purchased"))), round(num(r.get("item_revenue")), 2))
             for r in raw if is_ad(r)]

    # 2) Search query per transaction (missing ones show as "u")
    qrows = pull("googleanalytics4", GA4_ACCOUNT,
                 ["transactionid", "session_google_ads_query", "session_google_ads_campaign_name"], d_from, d_to)
    query = {str(r.get("transactionid")): str(r.get("session_google_ads_query") or "") for r in qrows if is_ad(r)}

    # 3) Spend + brand spend
    spend_rows = pull("google_ads", ADS_ACCOUNT, ["date", "spend", "clicks"], d_from, d_to)
    spend = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for r in spend_rows:
        x = spend[iso(r["date"])]
        x[0] += num(r.get("spend")); x[1] += int(num(r.get("clicks")))
    term_rows = pull("google_ads", ADS_ACCOUNT, ["date", "search_term", "spend", "clicks"], d_from, d_to)
    for r in term_rows:
        if any(m in str(r.get("search_term") or "").lower() for m in BRAND_MARKS):
            x = spend[iso(r["date"])]
            x[2] += num(r.get("spend")); x[3] += int(num(r.get("clicks")))

    # ---- checks ----
    if d_to not in spend:
        fail(f"No Google Ads spend row for {d_to} yet; leaving dashboard unchanged")
    if any(not (d_from <= l[0] <= d_to) for l in lines):
        fail("Purchase outside the requested window")
    for d, x in spend.items():
        if x[2] > x[0] + 0.01:
            fail(f"Brand spend exceeds total spend on {d}")
    totals = pull("googleanalytics4", GA4_ACCOUNT,
                  ["date", "item_revenue", "items_purchased", "session_google_ads_campaign_name"], d_from, d_to)
    want = defaultdict(lambda: [0.0, 0])
    for r in totals:
        if is_ad(r):
            want[iso(r["date"])][0] += num(r.get("item_revenue")); want[iso(r["date"])][1] += int(num(r.get("items_purchased")))
    got = defaultdict(lambda: [0.0, 0])
    for d, _, _, q, rev in lines:
        got[d][0] += rev; got[d][1] += q
    for d in set(want) | set(got):
        if abs(want[d][0] - got[d][0]) > 0.01 or want[d][1] != got[d][1]:
            fail(f"Day {d} does not reconcile: items {got[d]} vs totals {want[d]}")

    # ---- merge ----
    with open(DATA) as f:
        cur = json.load(f)
    kept = [p for p in cur.get("purchases", []) if p[0] < d_from]
    new = [[d, tx, item, q, rev, kind(query.get(tx)), query.get(tx, "") if kind(query.get(tx)) != "u" else ""]
           for d, tx, item, q, rev in lines]
    purchases = sorted(kept + new, key=lambda p: (p[0], p[1]))
    sp = {row[0]: row for row in cur.get("spend", []) if row[0] < d_from}
    for d, x in spend.items():
        sp[d] = [d, round(x[0], 2), x[1], round(x[2], 2), x[3]]
    if len(purchases) < len(kept):
        fail("Purchase count shrank unexpectedly")

    out = {"updated": dt.datetime.now(ZoneInfo("America/Phoenix")).replace(microsecond=0).isoformat(),
           "start": START, "through": d_to, "partyValue": cur.get("partyValue", 300),
           "purchases": purchases, "spend": [sp[d] for d in sorted(sp)]}
    with open(DATA, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    yd = [l for l in lines if l[0] == d_to]
    print(f"OK: {len(new)} purchases for {d_from}..{d_to}; yesterday {len(yd)} items, spend ${spend[d_to][0]:,.2f}.")


if __name__ == "__main__":
    main()
