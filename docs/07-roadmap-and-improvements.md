# 7. Roadmap, known limitations and improvements

_Reflects: 2026-10-20. Items here are **proposals** until they appear in [PROGRESS.md](PROGRESS.md)._

## 7.1 Phases
| # | Scope | Est. | Done when |
|---|---|---|---|
| 0 ✅ | Scaffold, config, SQLite, CLI, profile/answers | 2 d | `applypilot init` |
| 1 ✅ | GH/Lever/Ashby, seeding, tiered poller, alerts, launchd | 1 wk | Median posting → push < 5 min over 1,000+ boards *(1,162 boards; first live sample 1.9 min, n=1; see L-1)* |
| 2 🔶 | **Done:** URL → ATS resolver, GitHub new-grad lists, HN, company + title resolution, self-expanding boards. **Left (2b):** Gmail alert ingestion (LinkedIn, Indeed, Handshake, WAAS), URL → ATS resolver, GitHub new-grad lists, HN, SerpApi/JSearch; boards auto-added from alerts | 1 wk | A LinkedIn alert job resolved to its ATS page; new company added to polling |
| 3 🔶 | **3a done (2026-10-21):** description hard drops (citizenship/green card, clearance, ITAR, scam, 4+ yrs), sponsor/E-Verify/H-1B signals, knockouts, score 0–100. **3b left:** fit to 6 pre-built resume variants; batch mode for `/resumetailor` | 1 wk | ✅ labeled set (73 excerpts): 35/35 hard drops, 0/38 false drops, 0/12 "no sponsorship" keeps dropped |
| 4+ ✅ | Automation items, all built 2026-10-22: answer memory, grounded essay drafts, background preparation, phone one-tap approval (off until enabled + 5 supervised), daily digest with Gmail response tracking | | |
| 4 🔶 | **4a done (2026-10-22):** form readers, answers from the user's files, dry-run filler with readback. **4b built (2026-10-22):** `apply` at the Mac (fill + attach, red outlines for what needs the user, submit on "submit", confirmation detection, per-company cap, log, push), `queue`, `applications`. **Left:** the 20 live applications themselves; grounded essay drafts; phone approval (option a) later | 1.5 wk | 20 live applications, 0 wrong fields |
| 5 🔶 | Dashboard (plan approved 2026-10-22): **W1 read-only pages done**; W2 apply from the dashboard, W3 tracking + settings, W4 Tailscale/phone next. Then Workday source and adapter | 1.5 wk | 100 applications in one day |
| 6 | Gmail status sync, funnel metrics, generic JSON-LD career pages | later | Response rate by source / tier / variant |

## 7.2 Known limitations (current)
| # | Limitation | Impact | Planned fix |
|---|---|---|---|
| L-1 | **Detection latency barely measured**: one post-baseline relevant job so far (Anthropic, 1.9 min) | Can't claim the < 5 min median yet | Let the agent run a few days; `stats` reports p50/p95 |
| L-2 | ~~Relevance uses title and location only~~ Description screening built 2026-10-21 (§2.10). Still title-only for jobs **without a description** (L-19) | — | — |
| L-3 | **No description** for Greenhouse baseline jobs (fetched only for new relevant ones) and for **feed jobs on ATSs we don't poll** (1,448 relevant Workday/SmartRecruiters/other jobs) | Phase 3 filters can't judge them | Backfill `details` for fresh relevant jobs; fetch the posting page's JSON-LD for `other` |
| L-4 | Lever/Ashby slug hits **can't be verified** by name | Occasional wrong-company boards | Check job descriptions mention the company name or domain; auto-flag boards whose jobs are mostly non-US |
| L-5 | Most large H-1B sponsors (Amazon, Google, Microsoft, TCS…) use **Workday or their own ATS** | Many sponsor jobs are missed | Phase 5 Workday source; Phase 2 alert emails cover the rest |
| L-6 | Fresh baseline jobs appear only in `jobs --fresh` (no push), **unless a feed later surfaces them** (pushed since 2026-10-20) | A job posted shortly before its board was seeded gets no alert if no feed lists it | Optional one digest push per seeding run |
| L-7 | Only **additive** migrations (`db.MIGRATIONS` adds missing columns) | Renames or type changes still need a manual step | Add a `schema_version` table and ordered migrations when the first non-additive change comes |
| L-8 | The H-1B data is FY2023 (the archive's latest CSV) | Sponsor counts are ~2 years old | Download the newer export from the Employer Data Hub UI; add DOL LCA FY2025–26 |
| L-9 | The Mac has to be awake | Lid closed on battery = no polling | Optional: run the poller on a small always-on host (Phase 5 "cloud later") |
| L-10 | The `jobs` table grows without bound (~67K rows after day 1, raw JSON included) | DB size grows | Prune `raw` for irrelevant jobs; drop jobs closed more than 90 days ago |
| L-11 | **Feed-only jobs are never closed** (no board poll to notice they're gone) | Stale Workday/other jobs stay "open" | Close them when the list row turns `active: false`, or after N days |
| L-12 | **Probed boards from feeds can collide** (Lever/Ashby can't be name-checked); ~70 such boards start warm | A few wrong-company boards | Same as L-4; their jobs still go through the relevance gate |
| L-13 | A Simplify row can point at a **board the company abandoned** (e.g. `ashby/whatnot` 404s although the page loads) | A few 404s, then the board goes dead | Nothing needed; optionally probe the company again when a board dies |
| L-15 | **JSearch (free plan) is slow and thin**: each of 4 queries every ~16 h; the live index returned 0–10 jobs per query, all with a single aggregator link, some senior or scam-pattern | Low value: a few extra jobs/day, mostly needing manual apply | Keep as cheap coverage. Don't pay for Pro unless a week of `stats` shows it finds relevant jobs nothing else does. Gmail alerts (LinkedIn/Indeed) are the better next source. |
| L-16 | Resolving JSearch's aggregator-only results by company + title works only for companies on Greenhouse/Lever/Ashby (none of the first 10 were: Workday, IBM, AbbVie, Amazon, HEB…) | Most JSearch jobs keep an aggregator link | Phase 5 Workday source |
| L-17 | **Gmail sign-in lasts 7 days** while the Google Cloud app is in Testing (production needs a homepage + privacy-policy URL) | Alert ingestion stops weekly until `gmail login` is rerun | The ingester will push a "sign in again" alert on `invalid_grant`; or publish a small GitHub Pages site and switch to production |
| L-18 | **Most Indeed alert jobs don't resolve to an ATS we poll**: 4 of 59 on the first baseline (Deepgram on Ashby, Calendly and Nebius on Greenhouse); the rest are small employers, staffing firms or Workday | They're pushed with an Indeed link (manual apply, often "Easily apply" on Indeed) | Phase 5 Workday source; decode `cts.indeed.com` / `pagead` trackers offline for the employer URL (never fetch them) |
| L-19 | **4,317 relevant open jobs have no description** and can't be screened: Greenhouse baseline jobs older than 14 days, Workday/SmartRecruiters/other feed jobs, Indeed cards (snippet only) | Citizenship/clearance-only roles among them can still be pushed if a feed resurfaces them | `_known` fetches GH descriptions before a push (done); fetch JSON-LD from employer pages for `other`; Workday source (Phase 5) |
| L-20 | The labeled set was labeled by Claude (2026-10-21) from the key passages, not by the user, and the filters were tuned on it (two rounds) | 100% on it is optimistic | One held-out spot check done (B-015). The user should review a sample of `hard drop` reasons; grow the set from real misses |
| L-21 | One sentence can both offer and refuse sponsorship ("we support H-1B transfers and F-1 OPT … not able to sponsor new H-1B visas", Firecrawl) → stance `no` | Ranking slightly off for such jobs | Detect "supports OPT/STEM OPT / H-1B transfers" as its own positive signal |
| L-22 | Sponsorship stance found in only 144 of 2,292 descriptions (85 no, 59 offers); most say nothing | The visa score is mostly the "unknown" default | H-1B history (DOL LCA FY2025–26) per company as the main signal |
| L-23 | **`ats_score.py` bugs** (in the user's skill, not this repo), found by the batch runs: "B.S., M.S., or Ph.D." read as a PhD requirement (the comma); `title_words` takes the line after an "…AND ROLE:" header; "excited" matches `cited`; "$135,000" matches cost efficiency; "medical conditions" (EEO) matches healthcare | Fit and variant scores are off by a few points, mostly equally across variants | The user fixes the scorer or approves the edit. Claude Code's permission check blocked edits under `~/.claude/skills`, so the batch rules travel in the prompt instead |
| L-24 | Fit is the **keyword** ATS score, not semantic fit or what a recruiter thinks | A keyword-dense variant can win jobs where another reads better to a person | Phase 3b+: embedding similarity; a human picks the variant at approval time (Phase 4) |
| L-25 | `data` variant scores 78 on its archetype: ARIMA/Prophet, statsmodels, Snowflake, dbt, Airflow and MLflow are unconfirmed (not added) | Data-science jobs rank lower on fit | The user confirms which of these they have used (interactive `/resumetailor`), then rebuild `data` |
| L-26c | Response tracking matches by **company name** in Gmail; a common name (e.g. "X", "Ramp") can pull unrelated mail, and replies that don't name the company are missed | Outcome may be wrong or missing for some applications | Match the ATS sender domains (`greenhouse-mail.io`, `hire.lever.co`, `ashbyhq.com`) and the job title too |
| L-26b | Drafts are only as good as the fact base; a grounding check verifies cited phrases and numbers, not tone or relevance | A weak but grounded draft can pass | The user reviews every draft before typing `submit` |
| L-26a | Answer memory (2026-10-22) learns only from **confirmed** submissions, so it starts empty | Coverage gains appear after the first applications | — |
| L-26 | Only **67% of required fields** are answerable from the user's files (60 forms); 2 of 60 forms need nothing from the user. The rest: legal acknowledgements, essays, "how many years of X", office choices | Most applications need the user for 1–5 fields | 4b: fill what's known, the user completes the rest in the open window; optional standing decisions for repeated legal notices; grounded essay drafts |
| L-27 | Pages that redirect to the company's own site (e.g. Databricks) and non-GH/Lever/Ashby ATSs can't be filled | Manual apply | Phase 5 Workday; generic filler later |
| L-28 | Dates with only a month in the profile (graduation `2026-12`) are left to the user | One more manual field on Ashby | Store the exact expected graduation date in `profile.yaml` |
| L-14 | The GitHub list feed downloads **~2 MB gzipped each time the file changes** (at most every 5 min, ≤ ~600 MB/day worst case) | Bandwidth | Switch to the commits API + per-commit diff if this matters |

## 7.2a Phase 2b: what's left
| Item | Needs from the user | Plan |
|---|---|---|
| **Gmail alert ingestion** (LinkedIn, Indeed, Glassdoor, ZipRecruiter, Handshake, Wellfound, Built In, WAAS). **Sign-in + Indeed parsing live 2026-10-20** (`gmail:indeed` feed); other sites left | Done: OAuth client, `gmail login`, Indeed alerts. Left: LinkedIn alerts reaching this inbox (none in 30 days), then Glassdoor/ZipRecruiter/Dice/Handshake alerts; one sample per site → a parser each | A `gmail` feed: query by sender, parse each card's company + title + location (+ the "apply on company site" link when present), then `resolve()` / `resolve_by_name()`. Never fetches LinkedIn/Indeed pages. |
| **Google Jobs via JSearch** ✅ built 2026-10-20 | The JSearch key (`feeds set-key jsearch`) | Done: `feeds/jsearch.py`. Waiting for a key to verify live and record numbers. |
| Remote boards (RemoteOK, Remotive, Himalayas, WWR), Adzuna | Adzuna: free app id/key | Feeds of the same shape |
| Workday tenants seen in feeds | none | 882 Workday jobs already identify tenant + site; they become boards when the Phase 5 Workday source lands |

## 7.3 Improvement backlog (prioritized)
1. **Adaptive tiers.** Promote boards whose last relevant job was posted within 30 days, and demote
   hot boards that have had none for 30 days. Learn each company's posting hours and poll faster then.
2. **Freshness-first notifications.** Include the minutes since posting in the push (done), and send
   high priority only for new grad or for posts under 15 min old.
3. **Board-quality score.** Share of a board's jobs that are US tech roles, used to rank and to catch
   wrong-company boards automatically.
4. **More curated companies** in the user's target locations (Pittsburgh, Philadelphia, NJ,
   Charlotte, Cleveland).
5. **ETag-less fallback.** Hash the job-id list so unchanged 200s are cheap even where ETags are
   missing.
6. **Per-company application cap** and cooldown (Phase 4 requirement).
7. **Metrics export.** Daily latency report (p50/p95), relevant jobs per source, response rates once
   applying begins. These become resume-grade numbers.
8. **CI.** GitHub Actions running ruff + pytest on Linux. That also sidesteps the hidden-`.pth` issue.
