# 3. Tech stack and tools

_Reflects: Phase 2, part 1 (2026-10-20). Planned items are marked._

## 3.1 Runtime stack (in use)

| Choice | Version | Why this, not something else |
|---|---|---|
| **Python 3.12** | 3.12.13 (uv-managed) | Same language as the resume tooling (`ats_score.py`) and his other projects (ProspectPilot, OpsPilot, EvalForge). `asyncio` covers I/O-bound polling. |
| **uv** | 0.11 | Fast, reproducible installs with a lockfile (`uv.lock`); used in his other repos. |
| **httpx** (async) | ≥ 0.27 | Async HTTP with connection pooling and easy conditional headers; **respx** mocks it in tests. `aiohttp` would also work, but httpx matches his other projects. |
| **SQLite** (stdlib `sqlite3`, WAL) | — | Single-user and local; zero ops. WAL allows the CLI to read while the poller writes. Postgres would be overkill until there's a dashboard with concurrent writers. |
| **pydantic v2** | ≥ 2.8 | Validates `config.yaml` into typed `Settings`. |
| **PyYAML** | ≥ 6 | Human-edited config, profile and answers files. |
| **Typer + Rich** | — | CLI with sub-commands, tables and progress bars. |
| **ntfy.sh** | public server | Free phone push over plain HTTP POST; no account or SDK. The click-through opens the apply URL. |
| **osascript** | macOS | Native banners without any dependency. |
| **launchd + caffeinate** | macOS | Starts at login, restarts on crash (`KeepAlive`), and stops idle sleep from freezing the poller (see B-006). |

## 3.2 Dev tools

| Tool | Use |
|---|---|
| **pytest + pytest-asyncio** (auto mode) | 75 tests covering sources, poller, relevance, seeding, resolver, feeds, JSearch |
| **respx** | Mocks the ATS HTTP APIs, with fixtures trimmed from real responses |
| **ruff** | Lint (E, F, I, B, UP rules), line length 120 |
| **Makefile** | `test`, `lint`, `poll`, `run`, `stats`, `jobs`, `seed`; exports `PYTHONPATH=src` (B-005) |
| **Claude Code** | Pair-building; `/resumetailor` skill reused in Phase 3 |

## 3.3 External data sources

### In use (Phase 1–2)
| Source | Endpoint | Auth | Notes (verified 2026-10-20) |
|---|---|---|---|
| Greenhouse Job Board API | `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` · `/jobs/{id}` · `/boards/{slug}` | none | ETag/304 supported. List without `content` is ~456 KB vs 5.7 MB with it (Stripe). `first_published` is present. Unknown slug returns 404 JSON. The board endpoint returns a display `name`. |
| Lever Postings API | `GET https://api.lever.co/v0/postings/{slug}?mode=json` | none | ETag supported. `createdAt` in ms, `country`, `workplaceType`, `hostedUrl`, `applyUrl`. Unknown slug returns 404. No board name. |
| Ashby Posting API | `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | none | ETag/304 supported. One call returns all jobs with descriptions. `publishedAt`, `isListed`, `isRemote`, secondary locations with countries, compensation. Unknown slug returns 404 "Not Found". |
| yc-oss YC companies | `https://yc-oss.github.io/api/companies/all.json` | none | 10.6 MB, 6,311 companies; 1,485 `isHiring` + `Active`. Unofficial; refreshed daily from YC's Algolia index. |
| USCIS H-1B Employer Data Hub | `https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-2023.csv` | none | The archive's latest file is FY2023: 33,333 rows, 869 employers with ≥ 25 approvals. Columns: Employer, Initial/Continuing Approval/Denial, NAICS, Tax ID, State, City, ZIP. |
| GitHub new-grad lists (Simplify format) | `https://raw.githubusercontent.com/{repo}/dev/.github/scripts/listings.json` | none | The structured source of the README table. Fields: `id, company_name, title, url, locations, date_posted, date_updated, active, is_visible, sponsorship, category, degrees`. ETag/304 supported, `cache-control: max-age=300`. Simplify: 19,318 rows, 3,030 active, 13.2 MB (2.0 MB gzipped). `sponsorship` ∈ Other / Offers Sponsorship / Does Not Offer Sponsorship / U.S. Citizenship is Required. Active URLs: job-boards.greenhouse.io 290, jobs.ashbyhq.com 203, TikTok 197, jobs.lever.co 131, SmartRecruiters 109, many Workday tenants. |
| HN Algolia API | `hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring` (threads); `?tags=comment,story_{id}&numericFilters=created_at_i>{t}&hitsPerPage=1000` (new comments) | none | Top-level comments have `parent_id == thread id`. Oct 2026 thread: 394 comments, 266 top-level. Most-linked hosts: jobs.ashbyhq.com 64, grnh.se 53, job-boards.greenhouse.io 25. |
| Greenhouse embed redirect | `GET boards.greenhouse.io/embed/job_app?token={gh_jid}` | none | Not followed: one `301` whose `Location` carries `?for={board slug}`; `404` for an unknown id. Turns a career-page `?gh_jid=` link into the board slug without guessing. |
| JSearch (Google for Jobs) | RapidAPI: `GET https://jsearch.p.rapidapi.com/search` with `X-RapidAPI-Key` + `X-RapidAPI-Host: jsearch.p.rapidapi.com`. OpenWeb Ninja direct (keys from app.openwebninja.com): `https://api.openwebninja.com/jsearch/search` with `x-api-key`; on 404 the feed retries `/search-v2`. **Verified live 2026-10-20: `/search` works with `x-api-key`**; response `{status: "OK", request_id, parameters, data: [...]}` with the documented field names. On 2026-10-20 the rapidapi.com website timed out while `jsearch.p.rapidapi.com` still answered. | API key | Params: `query`, `page`, `num_pages` (10 results/page, 1 request each), `country`, `date_posted` (all/today/3days/week/month), `employment_types` (FULLTIME, CONTRACTOR, PARTTIME, INTERN), `job_requirements`, `remote_jobs_only`, `radius`. Job fields used: `job_id, job_title, employer_name, employer_website, job_apply_link, job_apply_is_direct, apply_options[{publisher, apply_link, is_direct}], job_city/state/country, job_location, job_is_remote, job_posted_at_timestamp, job_description, job_publisher`. Pricing per OpenWeb Ninja (2026): Free 200 requests/month, Pro $25 for 10,000, Mega $150 for 200,000. **Live findings (free plan, 2026-10-20):** each result had **one** apply option, always an aggregator (LinkedIn, Indeed, Jobgether, JobMESH, Remote.co, Snagajob, ClearanceJobs), never the employer link. `date_posted=today` returned 0 jobs for 3 of the 4 default queries; "entry level software engineer" over 3 days returned 3. "software engineer new grad" (today) returned 10, of which 3 were Senior/Staff/Principal. No rate-limit headers were returned. |
| Short links | `grnh.se`, `lnkd.in`, `bit.ly`, `t.co`, … | none | Followed one hop at a time (`follow_redirects=False`), up to 3 hops, stopping at the first non-shortener host. `grnh.se` → the Greenhouse job or a career page with `gh_jid`. |
| Gmail API | `https://gmail.googleapis.com/gmail/v1/users/me/{profile, messages?q=, messages/{id}?format=metadata\|raw}`; OAuth at `accounts.google.com/o/oauth2/v2/auth` → `oauth2.googleapis.com/token` | OAuth 2.0, Desktop client, loopback redirect `http://127.0.0.1:{port}/` + PKCE (S256), scope `gmail.readonly` | Sign-in built 2026-10-20 (`gmail.py`, plain httpx, no Google SDK). `access_type=offline` + `prompt=consent select_account` so a refresh token is always returned and the right account is picked. Testing-mode apps: Google revokes the refresh token after 7 days (`invalid_grant`). Publishing to production needs Branding homepage + privacy-policy URLs. Alert senders queried (`gmail.SITES`): LinkedIn `jobalerts-noreply@`/`jobs-noreply@linkedin.com`; Indeed `donotreply@jobalert.indeed.com` (saved-search alerts) and `donotreply@match.indeed.com` ("jobs you may like"; `indeedapply@indeed.com` is application receipts, not leads); Handshake `m.joinhandshake.com`; `glassdoor.com`, `ziprecruiter.com`, `dice.com`. `after:<epoch s>` filters by Gmail's receive time. **Indeed formats (live 2026-10-20):** plain-text cards `Title / Company - Location / [$salary] [tags] snippet [age] / link`; organic digest links are `indeed.com/rc/clk/dl?jk=<16 hex>`, sponsored ones `/pagead/clk/dl` (no job key), match emails `cts.indeed.com/v3/<opaque>`, "alert is active" notices `engage.indeed.com/f/a/<opaque>`. Last 30 days: 13 alerts + 17 matches; 7 days → 145 cards. |

### Planned
| Source | Phase | Access |
|---|---|---|
| Remote boards | 2 | RemoteOK, Remotive, Himalayas (JSON), We Work Remotely (RSS) |
| Adzuna | 2 | Free official API |
| Workday | 5 | Career-site JSON `…/wday/cxs/{tenant}/{site}/jobs` (POST, 20 per page, undocumented, polled gently) |
| SmartRecruiters, Workable, Recruitee, BambooHR, Jobvite, Rippling, Teamtailor, Breezy, iCIMS | 5+ | Public posting feeds, added by volume |
| Custom career pages | 6 | Sitemap + schema.org `JobPosting` JSON-LD |
| DOL OFLC LCA disclosure data | 3 | Sponsor signal by SOC code |
| E-Verify employer export | 3 | STEM OPT eligibility signal |

## 3.4 Planned stack for later phases
| Need | Choice | Phase |
|---|---|---|
| Resume tailoring | Headless Claude Code `claude -p "/resumetailor …"`, plus 6 pre-built variants scored with `~/.claude/skills/resumetailor/scripts/ats_score.py --json` | 3 |
| LLM calls (screening answers, field mapping) | Provider router copied from ProspectPilot `llm/` (Anthropic, Ollama, mock) | 3–4 |
| Browser automation | Playwright with a persistent dedicated Chrome profile, headed, N contexts | 4 |
| Review dashboard | FastAPI + HTMX on localhost, reached from the phone over Tailscale | 5 |
| Credentials (Workday accounts) | macOS Keychain via `keyring` | 5 |
