"""Job-alert emails, read from Gmail (read-only): one feed per site.

Each email's plain-text part is parsed into job cards (title, company, location, posted age). The
links in these emails are the sites' own click trackers and are never fetched: the runner finds the
employer's posting by company + title (`resolver.resolve_by_name`), and otherwise keeps a stable link
on the site for a manual look.

Indeed (formats seen 2026-10): alert digests from `jobalert.indeed.com` and "jobs you may like" from
`match.indeed.com`, both blocks of

    Title
    Company - Location
    [$salary] [tags such as "Easily apply"] snippet [posted age]
    <tracking link>

plus single-job matches (title, company and location on their own lines, then "View job: <link>").
"""

from __future__ import annotations

import email
import json
import re
import time
from email import policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from applypilot import gmail, notify
from applypilot.config import Settings, home
from applypilot.feeds.base import FeedResult, Lead

BASELINE_DAYS = 3  # the first fetch looks this far back (silently, as every feed's baseline)
_URL = re.compile(r"https?://\S+")
_URL_LINE = re.compile(r"^<?(https?://\S+?)>?$")
_AGE = re.compile(r"^(?:posted\s+)?(just posted|today|active today|(\d+)\+?\s+days?\s+ago)$", re.I)
_SALARY = re.compile(r"^(?:salary:\s*)?(?:from\s+|up to\s+)?\$[\d,.]+", re.I)
_TAGS = {"easily apply", "responsive employer", "urgently hiring", "hiring multiple candidates", "new",
         "be an early applicant", "you must create an indeed account before continuing to the company website"}


def _sent_at(msg: EmailMessage) -> float:
    try:
        return parsedate_to_datetime(msg["date"]).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _text(msg: EmailMessage) -> str:
    part = msg.get_body(("plain",))
    return part.get_content() if part else ""


def _blocks(text: str) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for line in text.splitlines():
        line = line.strip()
        if line:
            out[-1].append(line)
        elif out[-1]:
            out.append([])
    return [b for b in out if b]


def _indeed_url(link: str, title: str, company: str, location: str) -> str:
    """A stable Indeed link for the job: viewjob by job key when the tracker carries one, otherwise a
    search for the exact title at the company (the trackers differ in every email)."""
    jk = (parse_qs(urlsplit(link).query).get("jk") or [""])[0] if link else ""
    if re.fullmatch(r"[0-9a-f]{16}", jk):
        return f"https://www.indeed.com/viewjob?jk={jk}"
    return "https://www.indeed.com/jobs?" + urlencode({"q": f'"{title}" {company}', "l": location})


def _card(mid: str, n: int, sent: float, title: str, company: str, location: str, rest: list[str],
          link: str) -> Lead:
    posted, salary, tags, snippet = None, None, [], ""
    for line in rest:
        if m := _AGE.match(line):
            posted = sent - int(m.group(2) or 0) * 86400
        elif _SALARY.match(line):
            salary = line.removeprefix("Salary:").strip()
        elif line.lower() in _TAGS:
            tags.append(line)
        elif not _URL.search(line) and len(line) > len(snippet):
            snippet = line
    return Lead(
        item_id=f"{mid}:{n}",
        company=company,
        title=title,
        urls=[_indeed_url(link, title, company, location)],
        location=location,
        country="US",  # the alerts are US / US-remote searches; is_us still reads the location
        posted_at=posted or sent,
        description=snippet,
        raw={"email_id": mid, "salary": salary, "tags": tags, "sponsored": "/pagead/" in link},
    )


def parse_indeed(mid: str, msg: EmailMessage) -> list[Lead]:
    sent, blocks = _sent_at(msg), _blocks(_text(msg))
    leads: list[Lead] = []
    for i, b in enumerate(blocks):
        m = _URL_LINE.match(b[-1])
        if not m:
            continue
        # digests: the link ends the card; multi-job matches: the link stands alone after it
        card = b[:-1] if len(b) >= 3 else (blocks[i - 1] if len(b) == 1 and i else [])
        if len(card) < 2 or " - " not in card[1] or _URL.search(card[0]) or _URL.search(card[1]):
            continue
        company, location = (s.strip() for s in card[1].rsplit(" - ", 1))
        leads.append(_card(mid, len(leads), sent, card[0], company, location, card[2:], m.group(1)))
    if not leads:
        # single-job match: subject "Title @ Company", a block of title / company / location / details
        sm = re.match(r"^(.+?) @ (.+)$", msg["subject"] or "")
        view = next((u for b in blocks for line in b if line.startswith("View job:")
                     for u in _URL.findall(line)), "")
        for b in blocks:
            if sm and len(b) >= 3 and (b[0], b[1]) == (sm.group(1).strip(), sm.group(2).strip()):
                leads.append(_card(mid, 0, sent, b[0], b[1], b[2], b[3:], view.rstrip(">")))
                break
    return leads


PARSERS = {"indeed": parse_indeed}
LABELS = {"indeed": "Indeed", "linkedin": "LinkedIn", "handshake": "Handshake", "glassdoor": "Glassdoor",
          "ziprecruiter": "ZipRecruiter", "dice": "Dice"}


def _notices() -> dict:
    path = home() / "gmail_notices.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _set_notice(key: str, value: float) -> None:
    path = home() / "gmail_notices.json"
    path.write_text(json.dumps({**_notices(), key: value}))


class EmailAlerts:
    jobs = True

    def __init__(self, site: str, settings: Settings) -> None:
        self.site = site
        self.settings = settings
        self.name = f"gmail:{site}"
        self.label = LABELS.get(site, site)
        self.interval_s = settings.feeds.gmail_interval

    async def fetch(self, client: httpx.AsyncClient, etag: str | None, cursor: str | None) -> FeedResult:
        cur = json.loads(cursor) if cursor else {"since": time.time() - BASELINE_DAYS * 86400, "seen": []}
        try:
            g = gmail.Gmail(client)
            await self._remind(client)
            # after: is by Gmail's receive time; the overlap window plus "seen" ids covers same-second arrivals
            ids = await g.search(f"{gmail.SITES[self.site]} after:{int(cur['since'])}", max_results=50)
            leads: list[Lead] = []
            newest, window = cur["since"], {}
            for mid in reversed(ids):  # oldest first
                raw, received = await g.message(mid)
                window[mid] = received
                newest = max(newest, received)
                if mid not in cur["seen"]:
                    leads += PARSERS[self.site](mid, email.message_from_bytes(raw, policy=policy.default))
        except gmail.GmailAuthError as e:
            await self._expired(client, str(e))
            return FeedResult(401, error=str(e))
        since = newest - 300
        nxt = {"since": since, "seen": [m for m, t in window.items() if t >= since]}
        return FeedResult(200, cursor=json.dumps(nxt), leads=leads)

    async def _remind(self, client: httpx.AsyncClient) -> None:
        """Testing-mode apps lose the sign-in after 7 days: warn a day ahead, once per sign-in."""
        tok = gmail.token_info()
        if not (self.settings.feeds.gmail_testing and tok):
            return
        due = tok["obtained_at"] + (gmail.TESTING_TOKEN_DAYS - 1) * 86400
        if time.time() >= due and _notices().get("reminded") != tok["obtained_at"]:
            _set_notice("reminded", tok["obtained_at"])
            await notify.message(client, self.settings, "ApplyPilot: Gmail sign-in ends tomorrow",
                                 "Run `applypilot gmail login` on the Mac to keep reading job alerts.")

    async def _expired(self, client: httpx.AsyncClient, error: str) -> None:
        tok = gmail.token_info()
        if tok and _notices().get("expired") != tok["obtained_at"]:
            _set_notice("expired", tok["obtained_at"])
            await notify.message(client, self.settings, "ApplyPilot: Gmail sign-in expired",
                                 f"Job-alert emails are not being read. {error}", priority="high")
