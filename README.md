# ApplyPilot

Catch new job postings minutes after they go live, straight from company applicant-tracking systems
(Greenhouse, Lever, Ashby), before they're re-listed on LinkedIn or Indeed. Community new-grad lists
and HN "Who is hiring" cover the ATSs it can't poll, and keep adding new company boards. Built for an F-1 student's
search: early-career software/ML/data roles in the US. "No sponsorship" roles stay in scope, because
OPT plus STEM OPT covers about three years.

**Full documentation:** [docs/](docs/README.md) covers the idea, architecture, tech stack, data
schema, runbook, bugs and fixes, roadmap and progress log.

## How it works
- **Tiered conditional polling.**
  - Hot boards are polled every ~2.5 min, warm every 15 min, cold every 3 h.
  - Each poll sends `If-None-Match`, so an unchanged board costs one `304`.
  - A full cycle over ~100 boards takes a few seconds.
- **Baseline, then diff.**
  - A board's first scan is recorded silently. You get one digest of jobs posted in the last 48 h.
  - Every job ID that appears after that is new: it's judged right away and relevant ones are pushed.
- **Relevance gate.**
  - Title: software/ML/data, not senior/lead/manager, not an internship.
  - Location: US or US-remote.
  - A board that just posted a relevant role is promoted to the hot tier.
- **The board list grows from public data.**
  - A curated list, every actively hiring YC company (yc-oss dataset), and employers in the USCIS H-1B
    Employer Data Hub.
  - A prober finds which ATS each company uses.
- **Feeds grow it further.**
  - The SimplifyJobs new-grad lists (structured `listings.json`, conditional GET every 5 min), HN
    "Who is hiring", and Google Jobs via the JSearch API (optional key, paced to your monthly quota)
    are polled.
  - Every Greenhouse/Lever/Ashby link they contain becomes a polled board.
  - Jobs on Workday and other ATSs are stored and pushed under the same relevance gate.
  - A job seen by both a feed and its board is one alert.
- **Resolver.**
  - `applypilot resolve <link>` turns any job link (short links and career pages with `?gh_jid=`
    included) into the employer's ATS job.
  - `--company X --title Y` finds the ATS posting behind a LinkedIn/Indeed listing, probing and
    scanning the company if it isn't tracked yet.
- **Alerts** go to your phone through [ntfy](https://ntfy.sh) and to macOS notifications.

Only public, read-only job-board APIs are used. Nothing scrapes LinkedIn or Indeed, and nothing logs in.

## Quick start
```bash
uv sync
export PYTHONPATH=src            # see "macOS note" below
uv run applypilot init           # ~/.applypilot: config, db, profile/answers templates, ntfy topic
uv run applypilot seed curated   # probe seeds/companies.txt (hot tier)
uv run applypilot seed yc        # actively hiring YC companies (warm tier)
uv run applypilot seed h1b       # USCIS H-1B employers with >= 25 approvals
uv run applypilot poll           # one cycle (the first one is the silent baseline)
uv run applypilot feeds poll --force   # first fetch of the new-grad lists + HN (silent baseline, adds boards)
uv run applypilot jobs --fresh --hours 48
uv run applypilot agent install  # boards + feeds in the background (launchd, survives reboots)
uv run applypilot stats          # boards per tier, detection latency p50/p95
```
To get alerts on your phone, install the ntfy app and subscribe to the topic printed by `init`. The topic
is random, so treat it like a password.

**Managing boards:**
- `applypilot boards add greenhouse <slug>` adds a board by hand.
- `applypilot boards block lever neon` drops a board that belongs to a different company sharing the slug.

**macOS note:** uv marks `.venv` hidden here, and Python 3.12.13 skips hidden `.pth` files. That breaks
the editable install with `No module named applypilot`. The Makefile and the launchd agent set
`PYTHONPATH=src` for you.

## Roadmap
1. ✅ ATS discovery: Greenhouse, Lever and Ashby; tiered poller; seeding; alerts
2. 🔶 URL → ATS resolver, GitHub new-grad lists, HN, company+title resolution, Google Jobs via JSearch
   (done); Gmail job-alert ingestion (LinkedIn, Indeed, Handshake, Glassdoor, ZipRecruiter, Dice) next
3. Description filters: citizenship, clearance and ITAR hard drops; sponsorship/E-Verify signals; fit
   ranking against pre-built resume variants
4. Autofill: Playwright adapters that fill the form after you approve, never solve CAPTCHAs, and verify
   the confirmation
5. Phone dashboard and tracker; Workday adapter

## Tests
```bash
make test   # respx-mocked sources and feeds (fixtures trimmed from real responses), poller, resolver, relevance, seeding
```
