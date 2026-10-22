"""The ApplyPilot dashboard: a local web app over the same database the agent writes.

Served on 127.0.0.1 only (`applypilot web`, or the launchd agent `com.applypilot.web`). The phone reaches it
through `tailscale serve`, which keeps it inside the user's tailnet.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from applypilot import db, digest, fit, notify
from applypilot.config import get_settings, home
from applypilot.filters import assess
from applypilot.web import queries

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _age(ts) -> str:
    return notify.age(ts).removeprefix("posted ") if ts else ""


def _when(ts) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else ""


def _md(text: str) -> str:
    """The digest's small markdown subset (headings, bullets, bold, code) as HTML."""
    import html

    out, in_list = [], False
    for line in text.splitlines():
        esc = html.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"`(.+?)`", r"<code>\1</code>", esc)
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{esc[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("## "):
            out.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif line.strip():
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


templates.env.filters.update(age=_age, when=_when, md=_md, fromjson=lambda s: json.loads(s or "[]"))
VISA = {"offers": ("sponsors", "good"), "no": ("OPT path", "warn"), "unknown": ("", "")}
templates.env.globals.update(VISA=VISA)


def get_conn():
    """One connection per request, closed afterwards."""
    c = db.connect(get_settings().db_path, migrate=False)
    try:
        yield c
    finally:
        c.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})


def create_app() -> FastAPI:
    db.connect(get_settings().db_path).close()  # run migrations once at startup
    app = FastAPI(title="ApplyPilot", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home_page(request: Request, c: Conn):
        return page(request, "home.html", d=queries.home(c))

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, c: Conn, q: str = "", min_score: float = 0, days: float = 14,
                  ats: str = "fillable", visa: str = "", show_applied: bool = False, sort: str = "score", p: int = 1):
        rows, total = queries.jobs(c, q=q, min_score=min_score, days=days, ats=ats, visa=visa,
                                   hide_applied=not show_applied, sort=sort, page=p)
        f = {"q": q, "min_score": min_score, "days": days, "ats": ats, "visa": visa, "show_applied": show_applied,
             "sort": sort, "p": p}
        name = "_jobs_rows.html" if request.headers.get("HX-Request") else "jobs.html"
        return page(request, name, rows=rows, total=total, f=f, pages=max(1, -(-total // 50)))

    @app.get("/jobs/{job_id:path}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str, c: Conn):
        row = db.get_job(c, job_id)
        if row is None:
            raise HTTPException(404, "no such job")
        prep = db.get_prepared(c, job_id)
        evidence = assess(row["description"]).evidence if (row["reason"] or "").startswith("hard drop") else ""
        fits = {}
        if row["description"] and (fitter := fit.get(get_settings())) is not None:
            fits = dict(sorted(fitter.scores(fit.jd_text(row["title"], row["company"], row["description"])).items(),
                               key=lambda kv: -kv[1]))
        apps = c.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchall()
        return page(request, "job.html", j=row, prep=prep, ready=queries.readiness(prep) if prep else None,
                    evidence=evidence, fits=fits, apps=apps, flags=json.loads(row["flags"] or "[]"),
                    needs=json.loads(prep["needs"] or "[]") if prep else [],
                    drafts=json.loads(prep["drafts"] or "[]") if prep else [])

    @app.get("/applications", response_class=HTMLResponse)
    def applications_page(request: Request, c: Conn, status: str = ""):
        return page(request, "applications.html", apps=queries.applications(c, status), status=status)

    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application_page(request: Request, app_id: int, c: Conn):
        d = queries.application(c, app_id)
        if d is None:
            raise HTTPException(404, "no such application")
        return page(request, "application.html", **d)

    @app.get("/digest", response_class=HTMLResponse)
    def digest_page(request: Request, c: Conn):
        d = digest.build(c, get_settings())
        return page(request, "digest.html", d=d)

    @app.get("/files/{folder}/{name}")
    def files(folder: str, name: str):
        # screenshots only, from ApplyPilot's own folders; no paths
        if folder not in ("applications", "dryruns") or not re.fullmatch(r"[\w.-]+\.png", name):
            raise HTTPException(404)
        path = home() / folder / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    return app
