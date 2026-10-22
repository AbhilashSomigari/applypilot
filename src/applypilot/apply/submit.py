"""Apply to one job with the user at the Mac (Phase 4b, approval option "b").

The form is filled in a visible Chrome window (resume attached). Fields that need the user (legal
notices are always theirs, essays, unknown questions) and any field that failed to fill are outlined in
red, and listed in the terminal. Submit is clicked **only** after the user types `submit`. Then the
page must reach a confirmation (URL or "thank you for applying" text) before the application counts as
submitted. Validation errors send the user back to the form. Everything is logged in `applications`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from applypilot import db
from applypilot.apply import memory
from applypilot.apply.answers import Answer
from applypilot.apply.filler import _visible, css_str, fill_page, launch, open_page, read_value
from applypilot.config import Settings, home
from applypilot.seeds import company_key

CONFIRM_URL = re.compile(r"/confirmation|/thanks|/thank-you|submitted|application[-_]?complete", re.I)
CONFIRM_TEXT = re.compile(r"thank(s| you)[^.]{0,40}(for (applying|your (application|interest))|we.ve received)"
                          r"|application (was |has been )?(successfully )?(submitted|received)"
                          r"|successfully (submitted|applied)|we.ve received your application", re.I)
ERROR_TEXT = re.compile(r"(this field is|is) required|please (complete|fill|select|enter)|can.t be blank"
                        r"|invalid|missing", re.I)


def cap_reason(conn, settings: Settings, company: str, now: float | None = None) -> str | None:
    now = now or time.time()
    n = db.company_applications(conn, company_key(company), now - 30 * 86400)
    cap = settings.apply.max_per_company_30d
    return f"already {n} applications to {company} in 30 days (cap {cap})" if n >= cap else None


async def _highlight(page, ats: str, answers: list[Answer]) -> int:
    """Outline the fields the user has to complete; returns how many were found on the page."""
    found = 0
    for a in answers:
        key = a.field.key.rstrip("[]")
        sels = {"greenhouse": [f'[id="{css_str(key)}"]'], "lever": [f'[name="{css_str(a.field.key)}"]'],
                "ashby": [f'[name="{css_str(key)}"]', f'[name$="_{css_str(key)}"]', f'[id="{css_str(key)}"]']}[ats]
        for sel in sels:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.evaluate(
                    "e => { const t = e.closest('div,li,fieldset') || e; t.style.outline = '3px solid #e5484d';"
                    " t.style.outlineOffset = '4px'; }")
                found += 1
                break
    first = [a for a in answers if await _visible(page, ats, a)]
    if first:
        key = first[0].field.key.rstrip("[]")
        await page.locator(f'[id="{css_str(key)}"], [name="{css_str(first[0].field.key)}"]').first \
            .scroll_into_view_if_needed()
    return found


async def _page_values(page) -> dict:
    """What the form holds right before Submit (the record of what was sent)."""
    return await page.evaluate("""() => {
        const out = {};
        for (const e of document.querySelectorAll('input, textarea, select')) {
            const k = e.name || e.id; if (!k || e.type === 'hidden' || e.type === 'file') continue;
            if ((e.type === 'radio' || e.type === 'checkbox') && !e.checked) continue;
            out[k] = (out[k] ? out[k] + ', ' : '') + (e.value || '');
        }
        return out; }""")


async def _click_submit(page) -> bool:
    btn = page.get_by_role("button", name=re.compile(r"^\s*submit( (application|your application))?\s*$", re.I))
    if await btn.count() == 0:
        btn = page.locator("button[type=submit], input[type=submit]")
    if await btn.count() == 0:
        return False
    await btn.last.click()
    return True


async def wait_confirmation(page, timeout_s: float) -> tuple[str, str]:
    """('submitted', evidence) once the page confirms; ('errors', text) if the form complains; else timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        await page.wait_for_timeout(1000)
        if CONFIRM_URL.search(page.url):
            return "submitted", page.url
        body = await page.locator("body").inner_text()
        if m := CONFIRM_TEXT.search(body):
            return "submitted", m.group(0)
        alerts = page.locator("[role=alert], .error, [class*=error], [aria-invalid=true]")
        if await alerts.count():
            texts = [t.strip() for t in await alerts.all_inner_texts() if t.strip()]
            if any(ERROR_TEXT.search(t) for t in texts) or await page.locator("[aria-invalid=true]").count():
                return "errors", "; ".join(texts)[:300] or "fields marked invalid"
    return "timeout", page.url


async def _offer_to_remember(learn: list, company: str, ask, say) -> None:
    if not learn:
        return
    say("\nRemember these answers for the same question on future forms? (legal notices are never stored)")
    for i, (f, v) in enumerate(learn, 1):
        say(f"  {i}. {f.label[:80]}  ->  {v[:60]}")
    reply = (await ask("Enter = remember all · numbers to skip (e.g. 2,3) · n = none: ")).strip().lower()
    if reply in ("n", "no", "none"):
        return
    skip = {int(x) for x in re.findall(r"\d+", reply)}
    items = memory.load()
    kept = [memory.remember(f, v, company, items) for i, (f, v) in enumerate(learn, 1) if i not in skip]
    say(f"remembered {sum(m is not None for m in kept)} answer(s) in {memory.path()}")


async def apply(conn, settings: Settings, row, form, plan: list[Answer], *, ask=None, headless: bool = False,
                say=print) -> dict:
    """The whole interactive application for one job. Returns the stored application row as a dict."""
    ask = ask or (lambda prompt: asyncio.to_thread(input, prompt))
    out = home() / "applications"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    resume = next((a.value for a in plan if a.source == "resume"), None)
    app_id = db.add_application(conn, job_id=row["id"], company=row["company"],
                                company_key=company_key(row["company"]), title=row["title"], ats=row["ats"],
                                url=form.url, status="started", resume=resume, started_at=time.time())

    def finish(status: str, **kw) -> dict:
        db.update_application(conn, app_id, status=status, **kw)
        return dict(conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        ctx = await launch(p, headless)
        try:
            page = await open_page(ctx, form.url)
            results = await fill_page(page, row["ats"], plan, with_resume=True)
            by_key = {r.key: r for r in results}
            todo = [a for a in plan if a.source == "needs_you"
                    or by_key[a.field.key].status in ("failed", "mismatch")]
            await _highlight(page, row["ats"], todo)
            say(f"\n{row['company']}: {row['title']}\n{sum(r.status == 'filled' for r in results)} fields filled and"
                f" verified; resume: {Path(resume).name if resume else 'NONE (attach it in the window)'}")
            if form.captcha:
                say(f"This page uses {form.captcha}: if a challenge appears after Submit, solve it in the window.")
            if todo:
                say(f"Complete these {len(todo)} in the Chrome window (outlined in red):")
                for a in todo:
                    r = by_key[a.field.key]
                    why = r.error if r.status in ("failed", "mismatch") else a.note
                    req = "required" if a.field.required else "optional"
                    say(f"  - {a.field.label[:90]}  [{req}; {why}]")
            completed = [a.field.label for a in todo]
            while True:
                choice = (await ask("\nType 'submit' to send the application, or 'skip' to cancel: ")).strip().lower()
                if choice == "skip":
                    return finish("abandoned", answers=json.dumps([asdict(r) for r in results]))
                if choice != "submit":
                    continue
                values = await _page_values(page)
                # what the user entered for the questions they were asked (or changed), to offer to remember
                learn = []
                for a in plan:
                    if a.source in ("needs_you", "memory") or by_key[a.field.key].status in ("failed", "mismatch"):
                        got = await read_value(page, row["ats"], a.field)
                        prev = ", ".join(a.value) if isinstance(a.value, list) else (a.value or "")
                        if memory.storable(a.field, got) and got != prev:
                            learn.append((a.field, got))
                shot1 = out / f"{stamp}-{row['ats']}-filled.png"
                await page.screenshot(path=str(shot1), full_page=True)
                if not await _click_submit(page):
                    say("No Submit button found. Submit in the window yourself, then type 'submit' again to "
                        "check for the confirmation.")
                status, evidence = await wait_confirmation(page, settings.apply.confirm_timeout_s)
                if status == "errors":
                    say(f"The form reports problems: {evidence}\nFix them in the window.")
                    continue
                shot2 = out / f"{stamp}-{row['ats']}-after.png"
                await page.screenshot(path=str(shot2), full_page=True)
                record = {"answers": json.dumps({"filled": [asdict(r) for r in results], "page_values": values}),
                          "completed_by_user": json.dumps(completed), "screenshot_filled": str(shot1),
                          "screenshot_after": str(shot2)}
                if status == "submitted":
                    res = finish("submitted", submitted_at=time.time(), confirmation=evidence, **record)
                    await _offer_to_remember(learn, row["company"], ask, say)
                    return res
                say("No confirmation within the time limit. Check the window; the application is logged as "
                    "'unconfirmed'.")
                return finish("unconfirmed", submitted_at=time.time(), confirmation=evidence, **record)
        except Exception as e:
            return finish("failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            await ctx.close()
