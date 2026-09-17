#!/usr/bin/env python3
"""Estimate opencode model usage/cost across tailnet servers for the current month.

Reads /api/session (each entry carries whole-session cost + tokens), so no
per-message fetching is required. Usage is attributed to sessions *updated*
this month (covers continued tasks); cost is lifetime for those sessions.
"""
import json
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone

SEPT_START_MS = 1788220800000  # 2026-09-01T00:00:00Z in epoch millis
PAGE = 100

SERVERS = sys.argv[1:] or ["daniels-laptop.tail667900.ts.net",
                           "m1.tail667900.ts.net"]


def get(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "mynah-usage-report")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def all_sessions(host):
    base = f"https://{host}:4096/api/session?limit={PAGE}&order=desc"
    cursor, seen = "", 0
    while True:
        d = get(base + (f"&cursor={cursor}" if cursor else ""))
        rows = d["data"]
        for s in rows:
            t = s.get("time", {})
            created = t.get("created", 0) or 0
            updated = t.get("updated", 0) or 0
            if created >= SEPT_START_MS or updated >= SEPT_START_MS:
                s["_server"] = host.split(".")[0]
                loc = (s.get("location") or {}).get("directory") or s.get("projectID") or "global"
                s["_project"] = loc.split("/")[-1] or loc
                yield s
            if created < SEPT_START_MS and updated < SEPT_START_MS:
                seen = 1
                break
        if seen:
            break
        nxt = d["cursor"].get("next")
        if not nxt:
            break
        cursor = nxt


def main():
    agg = defaultdict(lambda: {"sessions": 0, "cost": 0.0,
                               "tokens": defaultdict(lambda: 0),
                               "servers": set(), "projects": set()})
    total_cost = 0.0
    total_sessions_updated = 0
    total_sessions_created = 0
    zero_tok = 0
    per_server = defaultdict(float)
    for host in SERVERS:
        got = list(all_sessions(host))
        total_sessions_updated += sum(1 for s in got)
        created_now = sum(1 for s in got if s["time"]["created"] >= SEPT_START_MS)
        total_sessions_created += created_now
        for s in got:
            tk = s.get("tokens") or {}
            if not int(tk.get("input") or 0) and not int(tk.get("output") or 0):
                zero_tok += 1
            model = s.get("model") or {}
            key = f"{model.get('providerID','?')}/{model.get('id','?')}"
            cost = float(s.get("cost") or 0)
            cache = tk.get("cache") or {}
            a = agg[key]
            a["sessions"] += 1
            a["cost"] += cost
            a["servers"].add(s["_server"])
            a["projects"].add(s["_project"])
            for f in ("input", "output", "reasoning"):
                a["tokens"][f] += int(tk.get(f) or 0)
            a["tokens"]["cache_read"] += int(cache.get("read") or 0)
            a["tokens"]["cache_write"] += int(cache.get("write") or 0)
            total_cost += cost
            per_server[s["_server"]] += cost

    elapsed_days = (datetime.now(timezone.utc).timestamp() * 1000 - SEPT_START_MS) / 86400000
    factor = 30.0 / elapsed_days

    print("=" * 72)
    print(f"opencode usage estimate  ·  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"scope: sessions active in September 2026 (updated this month) across: {', '.join(SERVERS)}")
    print(f"servers online now: [laptop ✓ m1 ✓ f101 ✗ offline]  (f101 has no sessions anyway)")
    print("=" * 72)
    print(f"total sessions updated this month : {total_sessions_updated}  (created in Sept: {total_sessions_created})")
    print(f"   of which zero-token (empty/stub) : {zero_tok}")
    print(f"total spend (lifetime of those sessions): ${total_cost:,.4f}")
    print(f"extrapolated to full month (x{factor:.1f}): ${total_cost * factor:,.4f}")
    print("per server:")
    for k, v in sorted(per_server.items()):
        print(f"   {k:16s} ${v:,.4f}")
    print("-" * 72)
    print(f"{'model':30s} {'sess':>4s} {'cost':>10s} {'in':>9s} {'out':>9s} {'re':>6s} {'cr':>9s}")
    for key in sorted(agg, key=lambda k: -agg[k]["cost"]):
        a = agg[key]
        tk = a["tokens"]
        print(f"{key:30s} {a['sessions']:4d} ${a['cost']:>9,.4f} "
              f"{tk['input']:>9,} {tk['output']:>9,} {tk['reasoning']:>6,} {tk['cache_read']:>9,}")
    print("-" * 72)
    print("per-project sessions:")
    proj = defaultdict(int)
    for key, a in agg.items():
        for p in a["projects"]:
            proj[p] += a["sessions"]
    for p, n in sorted(proj.items(), key=lambda x: -x[1]):
        print(f"   {n:4d}  {p}")


if __name__ == "__main__":
    main()