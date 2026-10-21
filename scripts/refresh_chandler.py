"""Daily refresh of ktr-chandler/data.json from Windsor.ai.

Chandler's dashboard is small, so every run rebuilds the whole file from the
start date (Sep 24, 2026, same as Scottsdale) through yesterday. That way late-posting
conversions and any Google Ads restatements always land, with no merge logic.
Any check failing exits non-zero without writing, so the dashboard keeps its
last good data and GitHub flags the failed run.

KTR Chandler rules (separate from every other KTR location):
- Parties: Google Ads account 896-966-3050, conversion action
  "KTR Chandler - Party Purchase" only (calls from ads excluded). Chandler's
  own GA4 property 422833205 does not tie purchases to ad clicks yet, so it
  is not used. Values are the online deposits; the page values each party at
  partyValue ($300), the same rule Scottsdale uses.
- Spend/clicks/campaigns: same Google Ads account.
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
DATA = os.path.join(os.path.dirname(__file__), "..", "ktr-chandler", "data.json")
ADS_ACCOUNT = "896-966-3050"
CONVERSION = "KTR Chandler - Party Purchase"
START = dt.date(2026, 9, 24)
PARTY_VALUE = 300


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def pull(fields, date_from, date_to):
    params = {
        "api_key": KEY,
        "date_from": date_from,
        "date_to": date_to,
        "fields": ",".join(fields),
        "select_accounts": ADS_ACCOUNT,
    }
    url = f"{API}/google_ads?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ktr-reports-refresh"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # never print the URL: it carries the key
        detail = e.read().decode(errors="replace")[:800].replace(KEY, "***")
        fail(f"Windsor request failed ({', '.join(fields)}): HTTP {e.code}: {detail}")
    except Exception as e:
        fail(f"Windsor request failed: {type(e).__name__}: {e}".replace(KEY, "***"))
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        fail(f"Unexpected Windsor response: {list(body)[:5] if isinstance(body, dict) else type(body).__name__}")
    print(f"google_ads {fields}: {len(rows)} rows")
    return rows


def ymd(v):
    s = str(v).replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        fail(f"Unexpected date value: {v!r}")
    return s


def num(v):
    return float(v or 0)


def fmt(v, places=2):
    v = round(float(v), places)
    return str(int(v)) if v == int(v) else f"{v:g}"


def main():
    if not KEY:
        fail("WINDSOR_API_KEY secret is missing")
    now = dt.datetime.now(ZoneInfo("America/Phoenix"))
    yesterday = now.date() - dt.timedelta(days=1)
    d_from, d_to = START.isoformat(), yesterday.isoformat()
    s_from, s_to = ymd(d_from), ymd(d_to)
    print(f"Window {d_from} .. {d_to}")

    # Spend, clicks and campaigns
    spend = defaultdict(lambda: [0.0, 0])
    camps = defaultdict(lambda: [0.0, 0])
    for r in pull(["date", "campaign", "spend", "clicks"], d_from, d_to):
        d = ymd(r["date"])
        s, c = num(r.get("spend")), int(num(r.get("clicks")))
        spend[d][0] += s
        spend[d][1] += c
        name = (r.get("campaign") or "").strip() or "(unnamed campaign)"
        camps[name][0] += s
        camps[name][1] += c

    # Purchases (one conversion action only)
    conv = defaultdict(lambda: [0.0, 0.0])
    for r in pull(["date", "conversion_action_name", "all_conversions", "all_conversions_value"], d_from, d_to):
        if (r.get("conversion_action_name") or "").strip() != CONVERSION:
            continue
        d = ymd(r["date"])
        conv[d][0] += num(r.get("all_conversions"))
        conv[d][1] += num(r.get("all_conversions_value"))

    # ---- checks ----
    if s_to not in spend:
        fail(f"No Google Ads spend row for {d_to} yet; leaving dashboard unchanged")
    if any(d < s_from or d > s_to for d in list(spend) + list(conv)):
        fail("Row outside the requested window")
    total_spend = sum(v[0] for v in spend.values())
    if abs(total_spend - sum(v[0] for v in camps.values())) > 0.01:
        fail("Campaign spend does not add up to daily spend")
    if os.path.exists(DATA):
        with open(DATA) as f:
            cur = json.load(f)
        old_spend = sum(float(x.split(",")[1]) for x in filter(None, cur.get("spend", "").split(";")))
        if total_spend + 1 < old_spend * 0.9:  # history only grows; a big drop means a bad pull
            fail(f"Total spend fell from ${old_spend:,.2f} to ${total_spend:,.2f}; leaving dashboard unchanged")

    new = {
        "updated": now.replace(microsecond=0).isoformat(),
        "through": d_to,
        "partyValue": PARTY_VALUE,
        "conversions": ";".join(f"{d},{fmt(conv[d][0])},{fmt(conv[d][1])}" for d in sorted(conv) if conv[d][0] or conv[d][1]),
        "spend": ";".join(f"{d},{fmt(spend[d][0])},{spend[d][1]}" for d in sorted(spend)),
        "campaigns": [{"name": n, "spend": round(v[0], 2), "clicks": v[1]}
                      for n, v in sorted(camps.items(), key=lambda kv: -kv[1][0]) if v[0] or v[1]],
    }
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w") as f:
        json.dump(new, f, separators=(",", ":"))

    y = conv.get(s_to, [0, 0])
    print(f"OK through {d_to}. Yesterday: {fmt(y[0])} parties, ${y[1]:,.2f} deposits, "
          f"${spend[s_to][0]:,.2f} spend. All time: {fmt(sum(v[0] for v in conv.values()))} parties "
          f"on ${total_spend:,.2f} spend.")


if __name__ == "__main__":
    main()
