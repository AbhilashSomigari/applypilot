# Progress log

Newest first. Each entry: date, phase, what changed, verified numbers.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold, config, SQLite, CLI, profile/answers templates | ✅ Done (2026-10-20) |
| 1 | Greenhouse/Lever/Ashby sources, tiered poller, seeding, alerts, launchd agent | ✅ Done (2026-10-20); agent installed and running |
| 2 | Gmail job alerts (LinkedIn, Indeed, Handshake), URL→ATS resolver, GitHub new-grad lists, HN, Google Jobs | 🔶 In progress: resolver, GitHub lists, HN, company+title resolution, Google Jobs (JSearch, live) done; Gmail sign-in + Indeed alerts live; LinkedIn and other alert sites left |
| 3 | Description filters (citizenship/clearance/ITAR), sponsorship and E-Verify signals, fit ranking, resume variants | 🔶 3a done (2026-10-21); 3b built (2026-10-22): 6 resume variants, fit in the score, headless tailoring |
| 4 | Playwright autofill, approve → submit, confirmation detection | 🔶 4a + 4b built (2026-10-22): plan, dry run, apply at the Mac; 20 live applications next |
| 5 | Phone dashboard, tracker, Workday adapter | Planned |
| 6 | Gmail status sync, funnel metrics | Planned |

---

## 2026-10-22: Grounded essay drafts (automation item 2)
- `apply/drafts.py`: headless Claude Code (no tools, JSON schema, $1 cap), facts limited to the
  verified sections of the resume fact base, grounding check on cited phrases and numbers. See
  docs/02 §2.15.
- `apply` drafts before filling and lists drafts for review; `applypilot draft <#>` previews them;
  `apply.drafts` setting.
- Live: Anthropic "Why Anthropic?" drafted in 18.5 s, 6 cited facts, checks passed.
- Also: the pre-rewrite backup branch and `refs/original` deleted (user's request).
- Tests: 177 (+7), plus 6 browser tests.

---

## 2026-10-22: README rewritten; pushed to a private GitHub repo
- README restructured: how it works (diagram), each stage, measured results, ground rules, setup,
  daily use, command reference, data locations, layout, development, roadmap.
- Whole history scanned for secrets and personal data before the first push: no ntfy topic, API key,
  OAuth client id or phone number in any commit, and no private files tracked.
- GitHub rejected the first push ("push declined due to email privacy restrictions": the account
  blocks pushes exposing the user's email). With the user's OK, the 6 unpushed commits were rewritten
  to the GitHub noreply address (`git filter-branch --env-filter`, content verified identical, dates
  kept). Commit hashes changed (d5721d4 → 19e2980, …) and references here were updated. The repo now
  commits with the noreply address. Pushed to a **private** repo.

---

## 2026-10-22: Answer memory, plus a numbered queue

**Asked by the user:** "how to automate everything". Plan agreed: start with item 1, remembering
answers. Later: grounded essay drafts, background pre-fill, phone approval, daily digest. Legal
notices stay "always ask"; no unattended submit and no CAPTCHA solving.

**Built:**
- `apply/memory.py`, `filler.read_value`, capture and offer in `apply`, reuse in `answers.plan`,
  `memory list/forget`. See docs/02 §2.14.
- `queue` numbers its rows (`apply 3`); the job id column wraps instead of truncating (the user's
  report: "job id column is only showing 10 characters followed by dots").
- Tests: 170 (+6), plus 6 browser tests (+1, end to end: learn after submit, reuse on a new form).
- Bug caught in testing: "referr" (for "referred by") matched inside "p**referr**ed office"; now
  word-bounded.

---

## 2026-10-22: Phase 4b, applying with the user at the Mac

**Decisions (user, 2026-10-22):** approve at the Mac (option b); legal notices always ask; commit 4a
(`3a1d063`).

**Built:**
- `apply/submit.py` (`applypilot apply JOB_ID`): cap and duplicate gates → fill with the resume →
  outline what needs the user → submit only on `submit` → confirmation by URL or text, validation
  errors back to the user → `applications` row, screenshots, phone push.
- `applications` table; `apply.*` settings; `queue` and `applications` commands; Rich emoji codes off
  (`ashby:snowflake:…` rendered as ❄).
- Tests: 164 (+1), plus 5 browser tests (+2: submit and confirm on local pages; validation error then
  skip).

**Not yet done:** no real application has been submitted. That's the user's first `apply` run.

---

## 2026-10-22: Phase 4a, reading application forms and dry-run filling (nothing submitted)

**Built:**
- `apply/forms.py`: one form shape for Greenhouse (`?questions=true`), Lever (apply-page HTML, card
  templates, hCaptcha detection) and Ashby (GraphQL `ApiJobPosting`).
- `apply/answers.py`: answers from `profile.yaml`/`answers.yaml` only; everything else "needs you" with
  the reason. Six empty `misc.*` keys were added to `answers.yaml` for the user to fill.
- `apply/filler.py`: Playwright + installed Chrome (dedicated profile). Fills and reads back every
  field, screenshots, never submits; no resume upload in dry runs.
- CLI `plan JOB_ID`, `fill JOB_ID [--keep-open] [--with-resume] [--headless]`.
- Tests: 163 (+38) plus 3 browser tests (`-m browser`), all from real form fixtures and labels.

**Measured:**
- 60 top-ranked forms read with 0 failures: 458 of 682 required fields (67%) answerable from the
  user's files.
- Reviewing every automatic answer found 9 kinds of wrong answers, all fixed (B-023).
- Dry runs with a fake identity (no real data typed into employer pages), 12 forms (4 per ATS) after
  fixes: 106 filled and verified, 0 mismatch, 0 failed. Databricks redirects to its own site
  (reported).

---

## 2026-10-21/22: Phase 3b, resume variants, fit and headless tailoring

**Built:**
- `tailor.py`: runs the user's `/resumetailor` skill with `claude -p … --output-format json` from the
  resume folder.
  - The batch rules (no questions; unconfirmed tools listed, not added; no writes outside the output
    folder; ≤ 6 rounds; a final `BATCH_RESULT {...}` line) are **in the prompt**. Editing the skill file
    was blocked by Claude Code's permission check, so it is unchanged.
  - Tools are pre-allowed narrowly; local repo copies are granted with `--add-dir` (B-019); the project
    venv is removed from `PATH`.
  - Timeout on a wall-clock deadline (B-022).
- `fit.py`: imports the skill's `ats_score.py` and scores every screened job against all six variants
  (PDF text and JD terms cached, 75 ms per job, B-021). It stores `jobs.fit`/`variant`; the score adds
  0–20 points; the push shows `fit 88 (ml-engineer)`.
- CLI: `variants build [--only …]`, `variants list`, `tailor JOB_ID`.
- Config `tailor.*` (resume dir, scorer, claude binary, timeout, parallel, repo_dirs).
- Deps `pdfminer.six`, `pypdf`. 124 tests passing, ruff clean.

**Variants** (archetypes are real postings from the job DB, by family size among 6,011 relevant
open jobs):

| Variant | Archetype | ATS score | Run |
|---|---|---|---|
| swe-backend | Role + requirement sections of 4 SWE new-grad postings (Notion, Harvey, Cursor, Affirm), 10K chars | 94 (the Notion-only first build scored 95 on Notion, archived) | 5.8 min, $2.22 |
| infra-platform | Solace Health, Associate Platform Engineer | 94 (93 before generic-izing) | 6.3 min, $2.24 |
| ai-engineer | Cognition, Applied AI Engineer | 92 | 5.4 min, $2.01 |
| fullstack-fde | SpruceID, Full-Stack SWE (New Grad) | 91 | 5.4 min, $1.67 |
| ml-engineer | Quora, MLE New Grad | 88 (scorer bugs; ~95 estimated by the session, not measured) | 4.4 min, $1.56 |
| data | Solace Health, Data Scientist | 78 (ARIMA/Prophet, statsmodels, Snowflake, dbt, Airflow, MLflow unconfirmed) | 4.7 min, $1.57 |

**Review of the variants:**
- All 1 page.
- Every term not in the master was checked against the fact base or the repos: Tailwind, Vite,
  WCAG/ARIA (Portfolio), VPC/Jaeger/RBAC (OpsPilot Terraform/tracing), "post-incident report"
  (OpsPilot README and `compose_report`), xarray (forecasting repo). Nothing fabricated.
- Two archetype leftovers fixed by hand: infra's header "relocating to Redwood City, CA" and data's PDF
  subject "Data Scientist - Solace Health".

**Fit backfill:**
- 1,745 jobs. ai-engineer was best for 1,079, ml-engineer 436, swe-backend 111, fullstack-fde 91,
  data 15, infra 12.
- swe-backend generalized poorly (B-020). Rebuilding it against 4 combined SWE postings first hit the
  usage limit, then battery sleep (B-022), then the 30-min timeout: the untrimmed 19.7K-char archetype
  spent 16 min before its first write. It succeeded with the role and requirement sections only (10K
  chars, `--timeout 3600`).
- After the rebuild, `screen --all` took 1 min 43 s for 1,779 jobs. Winners: ai-engineer 789,
  **swe-backend 546 (was 122)**, ml-engineer 368, fullstack-fde 53, infra 13, data 10. On plain
  "Software Engineer" titles: ai-engineer 246, swe-backend 167 (was 3 of 40 in the sample).

---

## 2026-10-21: First commit; Phase 3a description screening and ranking

**Commit:** `19e2980`, Phases 0–2 (56 files). Before it, `profile/profile.example.yaml` still held real
contact details; it now has placeholders (the real copy lives in `~/.applypilot/profile.yaml`).

**Built:**
- `filters.py` (`assess`): clause-and-section reading of a description. Hard drops: citizenship /
  green card, clearance, U.S. persons (ITAR), OPT training / bench-sales scheme, 4+ years required.
  Sponsorship stance, "now or in the future" wording and E-Verify are signals only. See docs/02 §2.10.
- `rank.py`: `score` (0–100: freshness, new grad, visa outlook incl. H-1B history, E-Verify, apply path,
  knockout penalties) and `screen` (stores `score, sponsorship, everify, flags, screened_at`; a hard
  drop sets `relevant=0`).
- Wired in before every push: poller, feed jobs, and known baseline jobs a feed resurfaces (which also
  fetch the Greenhouse description first). Push line, e.g. `… · score 64 · no sponsorship: OPT/STEM OPT
  path · E-Verify · via Simplify`.
- Title-level clearance drop in `relevance.py` (seen in Indeed alerts: "Cleared Software Engineer").
- CLI: `screen [--fetch-days N] [--all]` (re-judges titles too), `jobs --by-score` (score + visa columns),
  `stats` (screening counts; latency by board polls vs feeds).

**Eval (tests/fixtures/jd_eval.jsonl, 73 excerpts):**
- 70 live postings sampled across categories with a fresh seed, plus 3 synthetic (two Arya-style scams
  and a legitimate "STEM OPT program welcome" near miss). Labeled from the key passages before
  running the filter.
- First run 30/35 recall, 0 false drops (B-014); after fixes **35/35, 0/38 false drops, 0/12
  no-sponsorship keeps dropped**. Sponsorship stance matches on 72/73 (L-21).
- Held-out spot checks: 40 random drops all correct; 30 keyword-bearing keeps → 1 miss, fixed (B-015).

**Backfill (2026-10-21):**
- `screen --fetch-days 14` fetched 279 Greenhouse descriptions and screened 2,601 jobs in 17 s:
  1,744 kept, 857 hard drops.
  - Clearance 332: Wyetech 135, Palantir 52, Accenture Federal 25, CGS Federal 20, Anduril 15.
  - 4+ years 362; citizenship 131; ITAR 30.
  - Scam 2, of which one false (B-016, fixed) and one real: **Arya Groups**, the company that was in the
    user's Indeed alerts.
- 72 stale title verdicts demoted (B-018).
- Latency: board polls p50 1.5 min, p95 2.5 min (n=5); feeds p50 29.8 min (n=6); 2 re-listed/late
  jobs not counted (B-017).
- 116 tests passing (+25), ruff clean.

---

## 2026-10-20 (night): Gmail read-only sign-in

**Built:**
- `gmail.py`: OAuth for an installed app with plain httpx: loopback redirect on a random
  `127.0.0.1` port, PKCE (S256), `state` check, scope `gmail.readonly` only.
  - The refresh token goes to `~/.applypilot/gmail_token.json`, created with mode 600.
  - Refuses a sign-in that returns no refresh token or lacks the read scope.
  - `Gmail` async client: refreshes the access token when expired (or once on a 401). A revoked or
    7-day-expired sign-in (`invalid_grant`) raises `GmailAuthError`.
  - `SITES`: Gmail search per alert source (LinkedIn, Indeed, Handshake, Glassdoor, ZipRecruiter, Dice).
- CLI: `gmail login` (warns if the account differs from `profile.yaml`), `gmail status`,
  `gmail samples` (raw `.eml`, mode 600), `gmail logout` (revokes at Google).
- Tests: `tests/test_gmail.py` (7), including a fake browser hitting the real loopback server and a
  forged `state`. 85 passing, ruff clean.

**Setup state:** Desktop OAuth client created; the app stays in **Testing** because production
requires homepage and privacy-policy URLs (L-17).

**Then (same night): signed in, Indeed alerts live.**
- `gmail login` worked first time (read-only; token mode 600). The last 30 days had 38 Indeed emails
  (13 alerts, 17 "jobs you may like", 8 receipts/account), 3 Handshake account emails and **0 from
  LinkedIn**. Handshake now mails this inbox directly, so no forwarding filter is needed.
- `feeds/email_alerts.py`: an `EmailAlerts` feed per site with a parser (Indeed so far). It parses the
  plain-text part into cards, never opens tracking links, and resolves by company + title.
  - Stable links: `viewjob?jk=` from organic trackers, else an exact-title search. Duplicate cards
    across emails collapse to one job.
  - Pushes once on day 6 ("sign-in ends tomorrow") and once on expiry (`notify.message`).
  - `gmail login` now restarts the agent; `gmail test` previews parsed cards.
- Relevance: titles with cleared / clearance / TS/SCI / top secret / polygraph are dropped ("requires
  security clearance"), the user's hard drop. Seen live: "Cleared Software Engineer".
- **Live:** `gmail test --days 7` → 145 cards. Agent baseline: 59 cards → 45 new jobs (14 already known,
  same job across emails), 4 on an ATS we poll (L-18). 91 tests passing (+6), ruff clean.

## 2026-10-20 (late): Google Jobs via JSearch

**Built:**
- `feeds/jsearch.py`: one feed per query (4 defaults: new-grad and entry-level SWE, MLE, DS), US,
  full-time, recent window.
  - Pacing: `JSearch.interval_s()` keeps all queries within 90% of the monthly quota (free 200 →
    ~16 h per query; Pro 10,000 → ~19 min).
  - 429/401 back off through the feed error path; a warning is logged when fewer than 10 requests
    remain.
- `resolver.best_url` and `AGGREGATORS`: apply on the most direct option (pollable ATS → other ATS →
  employer page). LinkedIn/Indeed/Glassdoor/ZipRecruiter/Dice links are never fetched.
- Aggregator-only results go through `resolve_by_name` (company + title on the company's ATS board,
  probing and scanning it if untracked). Only if that fails is the aggregator link kept.
- Runner: a known job seen only on a silent baseline, relevant, fresh and never pushed is now pushed
  when any feed surfaces it.
- JSearch descriptions are stored on the job, which Phase 3 filters can use.
- CLI: `feeds set-key jsearch` (hidden prompt, config mode 600, restarts the agent), `feeds test
  jsearch`, quota pacing shown in `feeds list`.
- Tests: 70 → 75.

**Verified live (later the same evening):**
- RapidAPI's website timed out, so the key came from OpenWeb Ninja directly (`--provider
  openwebninja`); `/jsearch/search` with `x-api-key` works.
- The four baselines: one query returned 10 jobs, three returned 0 with `date_posted=today`. Four
  diagnostic requests showed the filters aren't the cause: the index is just thin (3 results over 3
  days for "entry level software engineer").
- Every result had a single aggregator apply link.
- Changes: on slow (free) pacing the feed now asks for `3days` (duplicates removed by job id), and
  ~15 more aggregator hosts were added (Jobgether, JobMESH, Remote.co, ClearanceJobs…).
- Tests: 75 → 78.
- Verdict: cheap coverage, low value on the free plan (docs/07 L-15).
- Requests used today: 1 test + 4 baselines + 4 diagnostics = 9 of 200.

---

## 2026-10-20 (evening): agent installed; Phase 2 part 1 (feeds + resolver)

**Agent:**
- `agent install`: launchd agent running under `caffeinate -i` (assertion verified with
  `pmset -g assertions`).
- Its first cycle covered 534 boards in ~6 s with 0 errors.
- Logging fixed before it could pile up (B-009): unbuffered `run.log`, httpx at WARNING, rotating
  `poller.log`, 5-min summaries.

**Built:**
- `resolver.py`:
  - `parse()` for Greenhouse (job, board, embed, API), Lever, Ashby (UUID-validated), Workday
    (tenant/site/requisition), SmartRecruiters, Workable, `gh_jid` / `ashby_jid` career-page embeds,
    and `other`.
  - Tracking params are stripped.
  - `resolve()` follows short links (grnh.se, lnkd.in, bit.ly…), recovers a Greenhouse slug from a
    bare `gh_jid` via the embed redirect, and registers pollable boards.
  - `find_on_boards` / `resolve_by_name` handle company + title matching (the LinkedIn path).
- `feeds/`:
  - `FeedRunner` loop beside the poller.
  - Feeds: `GitHubList` (Simplify-format `listings.json`, conditional GET) and `HackerNews` (Algolia,
    cursor on comment time).
  - Same baseline-then-diff semantics as boards. Feed jobs share the poller's job ids, so there's one
    row and one push per job.
- Schema: `jobs.source`, `feeds`, `feed_items`, additive `MIGRATIONS`.
- CLI: `feeds poll|list`, `resolve`, `run --no-feeds`, `stats` by source, `jobs` "via" column.
- Tests: 38 → 70, ruff clean.

**Live results (first feed baseline, 1 min 54 s):**
- 3,910 leads → 3,393 new jobs. 261 were already known from board polling, so dedupe works across
  sources.
- **510 boards added** by the feeds: 435 from GitHub lists (mostly hot), 73 from HN (warm).
- 848 companies probed.
- Boards: **653 → 1,162** (510 hot / 494 warm / 148 cold / 6 dead / 4 blocked).
- Relevant open jobs: 2,321 → **6,899**:
  - 5,135 first found by board polls, 1,764 first found by the lists;
  - **1,448 on ATSs we can't poll** (Workday, SmartRecruiters, Oracle, …).
- Feed rows judged not relevant: 1,235 non-tech titles, 54 London, 49 **"requires US citizenship"**
  (Simplify flag), 42 senior.
- `resolve --company Datadog --title "AI Research Scientist - Datadog AI Research"` matched the
  "(DAIR)" posting.
- `resolve --company Rubrik --title …`: Rubrik was untracked. The resolver probed the company, added
  `greenhouse/rubrik`, scanned it and returned the apply link. This is the Phase 2 "LinkedIn job →
  ATS page" check, done by hand. The Gmail ingester will feed it automatically.
- **First post-baseline detection:** Anthropic "Security Engineer / Research Engineer, RL Security",
  posted 19:17:54, detected and pushed 19:19:47 (**1.9 min**).

**Bugs found and fixed:** B-009 to B-013 (see [06-bugs-and-fixes.md](06-bugs-and-fixes.md)). Data
repairs:
- 2 wrong HN boards removed;
- all 2,361 cached probe misses re-probed;
- 118 placeholder boards re-demoted to cold.

**Not yet done (Phase 2b):** Gmail alert ingestion and Google Jobs (both need credentials from the
user), remote boards, Adzuna. See [07 §7.2a](07-roadmap-and-improvements.md).

---

## 2026-10-20: Phase 0 + 1 built and verified live

**Built:**
- Repo `~/Documents/applypilot`: Python 3.12 + uv, `src/` layout, 38 tests, ruff clean.
- `sources/`: Greenhouse, Lever and Ashby adapters over the public, keyless job-board APIs, with
  conditional GETs (ETag → 304).
- `poller.py`: tiered polling (hot 150 s / warm 900 s / cold 3 h, ±15% jitter), baseline-then-diff,
  close/reopen tracking, auto-promotion to hot, auto-demotion to cold, 404×3 → dead.
- `relevance.py`: title gate (tech role, not senior, not internship, not another engineering field)
  plus a US-location gate.
- `seeds.py`: slug prober with a per-company cache. Seeds: curated list (132 companies), YC
  (yc-oss), USCIS H-1B Employer Data Hub (FY2023).
- `notify.py`: ntfy phone push and macOS banner.
- `launchd.py`: per-user LaunchAgent under `caffeinate -i`.
- `cli.py`: init, boards add/list/block, seed curated/yc/h1b, poll, run, jobs, stats, agent,
  test-push.

**Live results:**
- Curated seeding: 132 companies → 110 boards in ~22 s; 32 companies aren't on these ATSs.
- YC seeding: 1,369 companies probed → 514 new boards (76 s).
- H-1B seeding: 843 employers probed → 29 new boards (49 s).
- Total **653 boards** (424 Ashby, 162 Greenhouse, 67 Lever); 27,810 open jobs, 2,321 judged relevant.
- **Full cycle over 649 active boards: 6.3 s, 0 errors**, 648 of them 304 Not Modified.
- Its first scan surfaced a Palantir "Software Engineer, New Grad" posted 22 h earlier, plus fresh
  roles at OpenAI, Anthropic, Cursor, Coinbase and Reddit.

**Bugs found and fixed:** B-001 to B-008 (see [06-bugs-and-fixes.md](06-bugs-and-fixes.md)).

**Not yet done (at the time):**
- The launchd agent wasn't installed yet (installed later the same day).
- No post-baseline detection-latency data yet.
