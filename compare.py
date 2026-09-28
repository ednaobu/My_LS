"""Measure the two architectures side by side and print a Markdown report.

    python compare.py

Numbers are from one laptop-class machine talking over localhost, so treat
them as relative, not absolute. Real networks make the microservice hops cost more.
"""
import statistics
import time
from pathlib import Path

import httpx

from stack import FINALIZE, ROOT, Stack

N = 200


def rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return float("nan")


def timed(fn, n=N) -> dict:
    for _ in range(10):
        fn()  # warm-up
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {"p50": statistics.median(samples), "p95": samples[int(n * 0.95) - 1]}


def loc(paths) -> int:
    return sum(len([l for l in p.read_text().splitlines() if l.strip()]) for p in paths)


def main() -> None:
    started = time.perf_counter()
    stack = Stack(base_port=28000, analyze_port=29000, data_dir=ROOT / ".data" / "compare").up(fresh=True)
    boot = time.perf_counter() - started
    try:
        api = httpx.Client(base_url=f"{stack.url('gateway')}/api", headers={"X-API-Key": "dev-key"})
        direct = httpx.Client(base_url=stack.url("agreement"))
        analyze = httpx.Client(base_url=stack.url("analyze"))

        intake = {"title": "Bench", "counterparty": "Globex Inc.", "requester": "b", "template_id": "tpl_msa",
                  "answers": {"effective_date": "October 1, 2026", "term_months": 12, "deal_value": 1000,
                              "auto_renew": False, "processes_personal_data": False, "governing_law": "Delaware"}}
        agreement_id = api.post("/intake", json=intake).json()["agreement_id"]
        analyze.post("/ingest/sync", json={"folder": str(ROOT / "analyze" / "samples")})
        contract_id = analyze.get("/contracts").json()[0]["id"]
        counter = iter(range(10**6))

        results = {
            "Read one record - Finalize via gateway (2 hops)": timed(lambda: api.get(f"/agreements/{agreement_id}")),
            "Read one record - Finalize service direct (1 hop)": timed(lambda: direct.get(f"/agreements/{agreement_id}")),
            "Read one record - Analyze monolith": timed(lambda: analyze.get(f"/contracts/{contract_id}")),
            "Create draft - Finalize intake (gateway -> template -> agreement)": timed(lambda: api.post("/intake", json=intake), 50),
            "Ingest + extract + classify + schedule - Analyze (1 transaction)": timed(
                lambda: analyze.post("/ingest", json={"text": f"Supply agreement between Acme Corp (A) and V{next(counter)} (B). "
                                                              "Fees $1,000. Term of 12 months effective as of 2026-01-01."}), 50),
        }
        finalize_rss = sum(rss_mb(stack.procs[n].pid) for n in FINALIZE)
        analyze_rss = rss_mb(stack.procs["analyze"].pid)
    finally:
        stack.down()

    finalize_loc = loc((ROOT / "finalize").rglob("*.py"))
    analyze_loc = loc((ROOT / "analyze").rglob("*.py"))
    print(f"""## Measured on this machine

| | Finalize (microservices) | Analyze (monolith) |
|---|---|---|
| Processes to run | {len(FINALIZE)} | 1 |
| Databases | {len(FINALIZE) - 1} (one per stateful service) | 1 |
| Resident memory | {finalize_rss:.0f} MB total | {analyze_rss:.0f} MB |
| Python LOC (non-blank) | {finalize_loc} | {analyze_loc} |

Whole stack boot (8 processes, to all-healthy): {boot:.1f}s

| Operation | p50 ms | p95 ms |
|---|---|---|""")
    for name, r in results.items():
        print(f"| {name} | {r['p50']:.1f} | {r['p95']:.1f} |")


if __name__ == "__main__":
    main()
