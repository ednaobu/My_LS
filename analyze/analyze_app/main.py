"""The single deployable: one FastAPI app exposing every Analyze module."""
import asyncio
import html
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, model_validator

from . import database, governing, ingestion, integrations, reporting, repository, tracking


@asynccontextmanager
async def lifespan(_app):
    database.init()
    task = asyncio.create_task(integrations.subscribe_forever())
    yield
    task.cancel()


app = FastAPI(title="LinkSquares Analyze (monolith)", lifespan=lifespan)


class Upload(BaseModel):
    filename: str | None = None
    content_base64: str | None = None
    text: str | None = None
    title: str | None = None
    counterparty: str | None = None

    @model_validator(mode="after")
    def one_source(self):
        if not (self.text or (self.filename and self.content_base64)):
            raise ValueError("provide text, or filename + content_base64")
        return self


class FolderSync(BaseModel):
    folder: str
    provider: str = "local"  # stand-in for s3 | google_drive | sharepoint | box | dropbox


class ReportDefinition(BaseModel):
    name: str
    audience: str = ""
    filters: dict = {}
    columns: list[str]


def _as_of(as_of: str | None) -> date:
    return date.fromisoformat(as_of) if as_of else date.today()


@app.get("/health")
def health():
    return {"service": "analyze-monolith", "status": "ok"}


# ---- ingestion & repository ----------------------------------------------------
@app.post("/ingest", status_code=201)
def ingest(upload: Upload):
    try:
        with database.transaction() as conn:
            if upload.text:
                text, confidence = ingestion.normalize(upload.text)
                contract, created = ingestion.ingest(conn, text=text, title=upload.title, filename=upload.filename,
                                                     counterparty=upload.counterparty, ocr_confidence=confidence)
            else:
                contract, created = ingestion.ingest_base64(conn, upload.filename, upload.content_base64,
                                                            title=upload.title, counterparty=upload.counterparty)
    except ingestion.UnreadableDocument as exc:
        raise HTTPException(415, str(exc)) from exc
    return {**contract, "created": created}


@app.post("/ingest/sync")
def sync(body: FolderSync):
    with database.transaction() as conn:
        return ingestion.sync_folder(conn, body.folder, body.provider)


@app.get("/contracts")
def contracts(contract_type: str | None = None, department: str | None = None, counterparty: str | None = None):
    with database.transaction() as conn:
        return repository.list_contracts(conn, contract_type, department, counterparty)


@app.get("/contracts/{contract_id}")
def contract(contract_id: str, include_text: bool = False):
    with database.transaction() as conn:
        found = repository.get(conn, contract_id, with_text=include_text)
    if not found:
        raise HTTPException(404, "contract not found")
    return found


@app.get("/contracts/{contract_id}/governing")
def governing_summary(contract_id: str):
    with database.transaction() as conn:
        try:
            return governing.current_terms(conn, contract_id)
        except KeyError:
            raise HTTPException(404, "contract not found") from None


@app.get("/search")
def search(q: str):
    return repository.search(q)


# ---- event tracking --------------------------------------------------------------
@app.get("/events/upcoming")
def upcoming(days: int = 90, as_of: str | None = None):
    with database.transaction() as conn:
        return tracking.upcoming(conn, _as_of(as_of), days)


@app.post("/events/scan")
def scan(as_of: str | None = None):
    with database.transaction() as conn:
        return {"alerts": tracking.scan(conn, _as_of(as_of))}


# ---- business intelligence ---------------------------------------------------------
@app.get("/dashboard")
def dashboard(as_of: str | None = None):
    with database.transaction() as conn:
        return reporting.dashboard(conn, _as_of(as_of))


@app.get("/reports")
def list_reports():
    with database.transaction() as conn:
        saved = [dict(r) for r in conn.execute("SELECT id, name, audience FROM reports")]
    return [{"id": k, "name": v["name"], "audience": v["audience"]} for k, v in reporting.PRESETS.items()] + saved


@app.post("/reports", status_code=201)
def create_report(body: ReportDefinition):
    with database.transaction() as conn:
        return reporting.save_report(conn, body.name, body.audience, body.filters, body.columns)


@app.get("/reports/{report_id}/run")
def run_report(report_id: str, format: str = "json", as_of: str | None = None):
    with database.transaction() as conn:
        report = reporting.get_report(conn, report_id)
        if not report:
            raise HTTPException(404, "report not found")
        rows = reporting.run_report(conn, report, _as_of(as_of))
    if format == "csv":
        return Response(reporting.to_csv(rows, report["columns"]), media_type="text/csv")
    return {"report": report, "rows": rows}


# ---- integrations ----------------------------------------------------------------
@app.post("/integrations/finalize/events")
def finalize_event(event: dict):
    return integrations.handle_finalize_event(event)


# ---- a tiny server-rendered dashboard ------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard_page(as_of: str | None = None):
    d = dashboard(as_of)
    top = max(d["value_by_department"].values(), default=0) or 1
    bars = "".join(
        f'<div class="row"><span>{html.escape(dep)}</span><div class="bar" style="width:{v / top * 100:.0f}%"></div>'
        f"<b>${v:,.0f}</b></div>" for dep, v in d["value_by_department"].items())
    deadlines = "".join(
        f"<tr><td>{e['due_date']}</td><td>{html.escape(e['kind'].replace('_', ' '))}</td><td>{html.escape(e['title'])}</td></tr>"
        for e in d["upcoming_deadlines"]) or '<tr><td colspan="3">None in the next 120 days</td></tr>'
    tiles = "".join(f'<div class="tile"><small>{k}</small><strong>{v}</strong></div>' for k, v in (
        ("Contracts", d["contracts"]), ("Active", d["active"]), ("Active value", f"${d['total_active_value']:,.0f}"),
        ("Expiring ≤90d", d["expiring"]["90_days"]), ("Auto-renewing", d["auto_renewing"])))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analyze Dashboard</title><style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--accent:#2f6fdb;--line:#e4e4df}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--card:#20201f;--accent:#6f9ff0;--line:#33332f}}}}
body{{margin:0;padding:24px 16px;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:880px;margin:auto}} h1{{font-size:22px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 20px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}}
.tile,section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.tile small{{display:block;color:var(--muted)}} .tile strong{{font-size:22px}}
section{{margin-bottom:16px}} h2{{font-size:15px;margin:0 0 10px}}
.row{{display:grid;grid-template-columns:110px 1fr auto;gap:10px;align-items:center;margin:6px 0}}
.bar{{height:10px;border-radius:5px;background:var(--accent)}} table{{width:100%;border-collapse:collapse}}
td{{padding:6px 4px;border-top:1px solid var(--line)}}</style></head><body><main>
<h1>Contract portfolio</h1><p>As of {d['as_of']} · governing (amended) terms</p>
<div class="tiles">{tiles}</div>
<section><h2>Active value by department</h2>{bars or '<p>No contracts yet</p>'}</section>
<section><h2>Upcoming deadlines</h2><table>{deadlines}</table></section>
</main></body></html>"""
