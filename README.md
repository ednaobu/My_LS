# LinkSquares-style CLM, built two ways

A working contract lifecycle management (CLM) platform based on the *LinkSquares Feature Overview*. It is built to show how microservices are put together and how they compare with a monolith:

* **Finalize** (pre-signature) is built as **microservices**: API gateway, event bus, and 5 domain services, each with its own database.
* **Analyze** (post-signature) is built as a **modular monolith**: one process, one database.
* Executed agreements flow from Finalize into Analyze through an event.

**Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).** It covers how each side is built, the patterns used, measured latency and memory, failure behaviour, and when to choose which.

## Quick start

```bash
pip install -r requirements.txt
python demo.py        # boots all 8 processes, runs the full lifecycle story, shuts down
python -m pytest      # 17 tests: monolith in-process + microservices end-to-end with failure injection
python compare.py     # measures both architectures, prints a Markdown report
```

To run it and explore:

```bash
python stack.py --fresh
# Finalize API: http://127.0.0.1:8000/api/...   (header  X-API-Key: dev-key)
#   per-service OpenAPI docs: http://127.0.0.1:8001/docs … :8005/docs, bus :8010/docs
# Analyze:      http://127.0.0.1:9000/  (dashboard)   http://127.0.0.1:9000/docs
python demo.py --attach   # run the story against the running stack
```

With Docker: `docker compose up --build` (the gateway is on :8000, Analyze on :9000).

## Feature map

| Feature (from the overview) | Where |
|---|---|
| Centralized template repository, dynamic Q&A drafting, request funneling | `finalize/services/template_service` |
| Conditional approval matrices, sequential/parallel sequences, auto status → Ready for Signature | `finalize/services/approval_service` + `agreement_service` event handlers |
| Negotiation hub, automated versioning (internal vs. external redlines), diffs, audit trail | `finalize/services/agreement_service` |
| AI abstract & summary, natural-language Q&A, playbooks with Smart Edits (track changes) | `finalize/services/review_service` |
| In-platform comments tied to the agreement, Slack notifications | `finalize/services/collaboration_service` |
| LS Sign / DocuSign / Adobe Sign (simulated) | `POST /api/agreements/{id}/sign` |
| Native API | `finalize/services/gateway` |
| Smart OCR, centralized searchable repository, cloud-storage sync | `analyze/analyze_app/ingestion.py`, `repository.py` |
| Smart Values extraction | `analyze/analyze_app/extraction.py` |
| Smart Administration (auto-categorization) | `analyze/analyze_app/categorization.py` |
| Governing summaries (amendment roll-up) | `analyze/analyze_app/governing.py` |
| Automated event tracking (renewals, notice deadlines) | `analyze/analyze_app/tracking.py` |
| Dashboards, cross-functional reports (JSON/CSV) | `analyze/analyze_app/reporting.py`, `GET /` |

"AI" features use deterministic heuristics so everything runs offline. Each one sits behind an interface where an LLM could be plugged in.

## Layout

```
finalize/                 microservices
  shared/lscommon/        infra-only shared lib: store, service bootstrap, events (outbox), rules
  services/<name>/main.py one FastAPI app per service
  Dockerfile
analyze/                  monolith
  analyze_app/            one FastAPI app, one module per feature
  samples/                sample contracts (MSA + amendment, NDA, lease, ...)
  Dockerfile
stack.py                  local process launcher (no Docker needed)
demo.py                   end-to-end story
compare.py                benchmark and footprint comparison
tests/                    pytest suites for both
docs/ARCHITECTURE.md      the analysis
```
