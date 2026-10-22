"""Fill an application form in Chrome from a plan, then read every field back. Never submits.

One locator strategy per ATS (from their live markup, 2026-10-22):
- Greenhouse (job-boards.greenhouse.io): inputs carry the API field name as `id`; selects are searchable
  comboboxes (click, type the option text, pick `[role=option]`).
- Lever: plain inputs by `name`; custom questions are real `<select>`, radio and checkbox inputs whose
  `value` is the option text.
- Ashby: text inputs by `name` = the field path; single selects are radio groups and multi selects
  checkboxes, each option a `<label for=…>`; booleans are Yes/No buttons inside the field's container.

Every fill is verified by reading the page state back; anything that doesn't match is reported, never
assumed. Submitting lives elsewhere (Phase 4b) and is never called from here.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from applypilot.apply.answers import Answer
from applypilot.config import home

PROFILE_DIR = "chrome-profile"  # a dedicated Chrome profile under ~/.applypilot, separate from the user's own


@dataclass
class FillResult:
    label: str
    key: str
    wanted: str
    got: str = ""
    status: str = "pending"  # filled (read back OK) | mismatch | failed | needs_you | skipped
    error: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _want(a: Answer) -> str:
    return ", ".join(a.value) if isinstance(a.value, list) else str(a.value or "")


async def _container(page, label: str):
    """The smallest element that contains the question text and a form control (Ashby/Lever fallback)."""
    text = label[:60]
    # a form <label> first: plain text also matches page chrome (Ashby's sidebar has its own "Location")
    loc = page.locator("form label, [class*=application] label, label", has_text=text).first
    if await loc.count() == 0:
        loc = page.get_by_text(text, exact=False).last
    if await loc.count() == 0:
        return None
    for _ in range(6):
        loc = loc.locator("xpath=..")
        if await loc.locator("input, textarea, select, button, [role=combobox]").count():
            return loc
    return None


# ---------- Greenhouse ----------
async def _gh(page, a: Answer, with_resume: bool) -> tuple[str, str]:
    f, key = a.field, a.field.key.rstrip("[]")
    el = page.locator(f"#{css_id(key)}")
    if f.kind == "file":
        if not with_resume:
            return "skipped", "resume not attached in a dry run"
        await el.set_input_files(a.value)
        await page.wait_for_timeout(800)
        return "filled", Path(a.value).name
    if f.kind in ("select", "multiselect"):
        values = a.value if isinstance(a.value, list) else [a.value]
        for v in values:
            await el.click()
            await el.fill(v[:40])
            opt = page.locator("[role=option]", has_text=re.compile(rf"^\s*{re.escape(v)}\s*$"))
            if await opt.count() == 0:
                opt = page.locator("[role=option]", has_text=v)
            await opt.first.click(timeout=5000)
        # the chosen label renders next to the combobox input
        box = el.locator("xpath=ancestor::*[contains(@class,'select__control')][1]")
        got = await box.inner_text() if await box.count() else ""
        return ("filled" if all(_norm(v) in _norm(got) for v in values) else "mismatch"), got.strip()
    await el.fill(_want(a))
    return _check(_want(a), await el.input_value())


# ---------- Lever ----------
async def _lever(page, a: Answer, with_resume: bool) -> tuple[str, str]:
    f = a.field
    sel = f'[name="{f.key}"]'
    if f.kind == "file":
        if not with_resume:
            return "skipped", "resume not attached in a dry run"
        await page.locator(sel).set_input_files(a.value)
        await page.wait_for_timeout(1500)  # Lever parses the resume and may prefill fields
        return "filled", Path(a.value).name
    els = page.locator(sel)
    tag = await els.first.evaluate("e => e.tagName + ':' + (e.type || '')")
    if tag.startswith("SELECT"):
        await els.first.select_option(label=_want(a))
        got = await els.first.evaluate("e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''")
        return _check(_want(a), got)
    if tag.endswith(("radio", "checkbox")):
        values = a.value if isinstance(a.value, list) else [a.value]
        for v in values:
            await page.locator(f'{sel}[value="{css_str(v)}"]').check()
        got = await page.eval_on_selector_all(f"{sel}:checked", "es => es.map(e => e.value).join(', ')")
        return ("filled" if all(_norm(v) in _norm(got) for v in values) else "mismatch"), got
    await els.first.fill(_want(a))
    return _check(_want(a), await els.first.input_value())


# ---------- Ashby ----------
async def _ashby(page, a: Answer, with_resume: bool) -> tuple[str, str]:
    f = a.field
    if f.kind == "file":
        if not with_resume:
            return "skipped", "resume not attached in a dry run"
        await page.locator(f'[id="{css_str(f.key)}"]').set_input_files(a.value)
        await page.wait_for_timeout(1500)
        return "filled", Path(a.value).name
    if f.kind in ("select", "multiselect"):
        values = a.value if isinstance(a.value, list) else [a.value]
        kind = "checkbox" if f.kind == "multiselect" else "radio"
        for v in values:
            ok = await page.evaluate(
                """([key, kind, want]) => {
                    const inputs = [...document.querySelectorAll(`input[type=${kind}]`)]
                        .filter(e => (e.name || '').endsWith('_' + key) || (e.id || '').includes('_' + key + '-'));
                    for (const e of inputs) {
                        const lab = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
                        if (lab && lab.innerText.trim().toLowerCase() === want.trim().toLowerCase()) {
                            if (!e.checked) lab.click(); return true; }
                    }
                    return false; }""", [f.key, kind, v])
            if not ok:
                return "failed", f"option {v!r} not found"
        got = await page.evaluate(
            """([key, kind]) => [...document.querySelectorAll(`input[type=${kind}]:checked`)]
                .filter(e => (e.name || '').endsWith('_' + key) || (e.id || '').includes('_' + key + '-'))
                .map(e => { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
                            return l ? l.innerText.trim() : e.value; }).join(', ')""", [f.key, kind])
        return ("filled" if all(_norm(v) in _norm(got) for v in values) else "mismatch"), got
    if f.kind == "boolean":
        box = await _container(page, f.label)
        if box is None:
            return "failed", "question not found on the page"
        btn = box.get_by_role("button", name=re.compile(rf"^{a.value}$", re.I))
        await btn.first.click()
        pressed = await btn.first.evaluate(
            "e => e.getAttribute('aria-pressed') === 'true' || /active|selected/i.test(e.className)")
        shown = "selected" if pressed else "not shown as selected"
        return ("filled" if pressed else "mismatch"), f"{a.value} ({shown})"
    if f.kind in ("location", "education"):
        box = await _container(page, f.label)
        inp = box.locator("input:not([type=file]):not([type=radio]):not([type=checkbox])").first \
            if box is not None else None
        if inp is None:
            return "failed", "question not found on the page"
        first = _want(a).split(",")[0].strip()
        await inp.fill(first)
        any_opt = page.locator("[role=option]").first
        try:
            await any_opt.wait_for(timeout=4000)
        except Exception:  # a plain text field: no suggestions, so the full text is the answer
            await inp.fill(_want(a))
            return _check(_want(a), await inp.input_value())
        # only a suggestion that names the same place; picking the top hit blindly could choose another city
        opt = page.locator("[role=option]", has_text=re.compile(re.escape(first), re.I)).first
        if await opt.count() == 0:
            return "mismatch", f"suggestions shown, none matching {first!r}"
        await opt.click()
        await page.wait_for_timeout(300)
        shown = (await inp.input_value()) or (await box.inner_text())
        return ("filled" if _norm(first) in _norm(shown) else "mismatch"), shown.strip()[:80]
    el = page.locator(f'[name="{f.key}"]')
    if await el.count() == 0:
        return "failed", "field not found on the page"
    await el.first.fill(_want(a))
    return _check(_want(a), await el.first.input_value())


def _same_site(a: str, b: str) -> bool:
    from urllib.parse import urlsplit

    host = lambda u: ".".join(urlsplit(u).netloc.lower().split(".")[-2:])  # noqa: E731
    return host(a) == host(b)


def css_id(s: str) -> str:
    return re.sub(r"([^\w-])", r"\\\1", s)


def css_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _check(want: str, got: str) -> tuple[str, str]:
    return ("filled" if _norm(want) == _norm(got) else "mismatch"), got


FILLERS = {"greenhouse": _gh, "lever": _lever, "ashby": _ashby}


async def _visible(page, ats: str, a: Answer) -> bool:
    key = a.field.key.rstrip("[]")
    if ats == "greenhouse":
        loc = page.locator(f"#{css_id(key)}")
    elif ats == "lever":
        loc = page.locator(f'[name="{a.field.key}"]')
    else:
        # Ashby ids are UUIDs that can start with a digit, which "#id" selectors can't express
        loc = page.locator(f'[name="{css_str(key)}"], [name$="_{css_str(key)}"], [id="{css_str(key)}"]')
    return await loc.count() > 0 and await loc.first.is_visible()


async def open_page(ctx, url: str):
    """Open the application in the session's tab and wait for the form."""
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    # wait for the form itself; some boards never go network-idle (chat widgets, analytics)
    await page.locator("form input, input[name], input[id]").first.wait_for(timeout=30_000)
    await page.wait_for_timeout(1500)
    if not _same_site(url, page.url):
        raise RuntimeError(f"redirected to {page.url}: the company hosts its own application page")
    return page


async def fill_page(page, ats: str, plan: list[Answer], with_resume: bool) -> list[FillResult]:
    """Fill every planned answer on an open page and read each one back."""
    results: list[FillResult] = []
    filler = FILLERS[ats]
    for a in plan:
        r = FillResult(a.field.label[:80], a.field.key, _want(a))
        if a.source == "needs_you":
            r.status, r.error = "needs_you", a.note
        elif a.source == "skip" or a.value in (None, "", []):
            r.status = "skipped"
        else:
            try:
                if not a.field.required and not await _visible(page, ats, a):
                    r.status, r.error = "skipped", "not shown on the page (depends on another answer)"
                    results.append(r)
                    continue
                r.status, r.got = await filler(page, a, with_resume)
            except Exception as e:  # one bad field must not stop the rest; it's reported
                r.status, r.error = "failed", f"{type(e).__name__}: {str(e)[:160]}"
        results.append(r)
    return results


def launch(p, headless: bool):
    return p.chromium.launch_persistent_context(
        str(home() / PROFILE_DIR), channel="chrome", headless=headless, viewport={"width": 1280, "height": 900})


async def fill(ats: str, url: str, plan: list[Answer], *, with_resume: bool = False, headless: bool = False,
               keep_open: bool = False, out_dir: Path | None = None) -> tuple[list[FillResult], Path]:
    """Dry run: open `url` in the dedicated Chrome profile, fill every planned answer, verify, screenshot.
    No submit."""
    from playwright.async_api import async_playwright

    out_dir = out_dir or home() / "dryruns"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    async with async_playwright() as p:
        ctx = await launch(p, headless)
        page = await open_page(ctx, url)
        results = await fill_page(page, ats, plan, with_resume)
        shot = out_dir / f"{stamp}-{ats}.png"
        await page.screenshot(path=str(shot), full_page=True)
        (out_dir / f"{stamp}-{ats}.json").write_text(json.dumps(
            {"url": url, "results": [asdict(r) for r in results]}, indent=1))
        if keep_open and not headless:
            input("Review the form in Chrome. Nothing has been submitted. Press Enter to close… ")
        await ctx.close()
    return results, shot
