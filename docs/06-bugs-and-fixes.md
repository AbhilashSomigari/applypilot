# 6. Bugs, errors and fixes

_Every defect found so far, with how it showed up, the root cause, and the fix. Newest last. Debugging
narratives and interview versions live in the local engineering journal._

| ID | Date | Area | Status |
|---|---|---|---|
| B-001 | 2026-10-20 | Inspiration research | Resolved (stale assumption) |
| B-002 | 2026-10-20 | Tests / HTML parsing | Resolved |
| B-003 | 2026-10-20 | Seeding: slug collisions | Mitigated |
| B-004 | 2026-10-20 | Seeding: empty placeholder boards | Resolved (auto-demote) |
| B-005 | 2026-10-20 | Environment: hidden `.pth` | Mitigated (workaround) |
| B-006 | 2026-10-20 | Runtime: Mac idle sleep | Resolved (caffeinate) |
| B-007 | 2026-10-20 | Seeding: H-1B name matches | Mitigated |
| B-008 | 2026-10-20 | Config: unused setting | Resolved |
| B-009 | 2026-10-20 | Agent logging: empty run.log, oversized poller.log | Resolved |
| B-010 | 2026-10-20 | Seeding: domain root from the wrong host label | Resolved (+ data repair) |
| B-011 | 2026-10-20 | Seeding: "X Job Board" names rejected; negatives cached forever | Resolved |
| B-012 | 2026-10-20 | Tiers: re-seeding undid cold demotion | Resolved (+ data repair) |
| B-013 | 2026-10-20 | Feeds: double push for two listings of one job (design review) | Prevented |
| B-014 | 2026-10-21 | Filters: hard drops missed on the labeled set (5 of 35) | Resolved |
| B-015 | 2026-10-21 | Filters: held-out miss, ITAR clause read as "preferred" | Resolved |
| B-016 | 2026-10-21 | Filters: "post-training feedback" read as a training-fee scam | Resolved |
| B-017 | 2026-10-21 | Stats: detection latency mixed board polls, feeds and re-listings | Resolved |
| B-018 | 2026-10-21 | Data: stored relevance verdicts predate newer title rules | Resolved (screen re-judges) |
| B-019 | 2026-10-21 | Tailoring: headless session couldn't read the local repo copies | Resolved (`--add-dir`) |
| B-020 | 2026-10-21 | Variants: one-posting archetype generalized poorly (swe-backend) | Resolved (multi-posting archetype: 122 → 546 jobs won) |
| B-021 | 2026-10-21 | Fit: backfill too slow (JD terms re-derived per variant) | Resolved |
| B-022 | 2026-10-22 | Tailoring: a run that slept mid-session hung 5.3 h past its 30-min timeout | Resolved (wall-clock deadline) |
| B-023 | 2026-10-22 | Answers: 9 kinds of wrong automatic answers found by reviewing 60 real forms | Resolved (+34 regression tests) |
| B-024 | 2026-10-22 | Filler: readback passed a wrong answer ("Doe" for preferred first + last name) | Resolved; lesson: readback ≠ correctness |
| B-025 | 2026-10-22 | Filler: Ashby UUID ids broke `#id` selectors; sidebar text matched instead of the field | Resolved |

---

### B-001: Lever probe for Plaid returned 404
- **Symptom:** While checking API shapes, `api.lever.co/v0/postings/plaid` returned 404.
- **Cause:** Plaid has moved off Lever; it's on Ashby now (confirmed later: `ashby/plaid`, 129 jobs).
- **Fix / lesson:** Don't hard-code which ATS a company uses. The prober checks all three and caches
  the result.

### B-002: Description test failed: `'<' in description`
- **Symptom:** `test_greenhouse_list_and_details` failed with leftover `</strong&g` text.
- **Cause:** A test artifact. The fixture was made by slicing the real content to 1,500 chars, which
  cut an escaped tag in half. The second failure (`"\n\n"` vs `"\n"`) was a wrong expectation, since
  two adjacent block tags correctly produce a paragraph break.
- **Fix:** Trimmed the fixture at a tag boundary (`&lt;/p&gt;`) and corrected the expected output.
  `html_to_text` handles Greenhouse's **double-escaped** HTML (`&lt;p&gt;`) by unescaping before
  stripping tags.

### B-003: Slug collisions: boards belonging to a different company
- **Symptom:** After seeding, `greenhouse/figure` (seed "Figure AI") listed fintech jobs (SEC
  reporting, treasury), and `lever/neon` (seed "Neon", the database) listed Brazilian bank jobs
  ("Banco de Talentos", "Remoto").
- **Cause:** Slugs are guessed. Figure AI's website `figure.ai` gave the domain root `figure`, which
  was tried **before** `figureai` and hit Figure Technologies' board. Lever and Ashby return no board
  display name, so a hit can't be verified.
- **Fix:**
  1. Candidate order changed to joined name → hyphenated name → domain root (`seeds.slug_candidates`).
     Figure AI now resolves to `greenhouse/figureai`.
  2. Removed the "first word only" candidate (e.g. `scale` for Scale AI) entirely.
  3. Added a `blocked` tier and `boards block`; `add_board` never resurrects a blocked board.
  4. Treat "Remoto/Brasil" as non-US.
- **Residual risk:** Lever/Ashby collisions are still possible → sample titles (runbook §5.5) and block.

### B-004: Big companies have empty Ashby boards
- **Symptom:** `ashby/figma`, `ashby/reddit`, `ashby/carta`, `ashby/confluent`, `ashby/mercury` and
  `ashby/figure` exist (200) but have 0 jobs; the real jobs are on Greenhouse.
- **Cause:** Placeholder or abandoned boards on the vendor.
- **Fix:** After a board's baseline, if no job is US-plausible (`is_us` not False), the board is
  demoted to **cold** (3-hourly) instead of being polled every 2.5 min. 13 curated boards were demoted
  on the first run.

### B-005: `ModuleNotFoundError: No module named 'applypilot'`
- **Symptom:** Tests and the `applypilot` console script failed after a rebuild (e.g. after editing
  `pyproject.toml`). It worked earlier in the same session.
- **Investigation:** `ls -lO .venv/.../site-packages` showed `_editable_impl_applypilot.pth` with the
  `hidden` flag, as was everything else in `.venv`. `chflags nohidden` fixed it only until the next
  rebuild. ProspectPilot's and evalforge's notes record the identical issue.
- **Cause:** uv 0.11 marks `.venv` hidden on this Mac and new files inherit the flag. Python 3.12.13
  **skips hidden `.pth` files**, so the editable install's path entry is never added.
- **Fix (workaround):**
  - pytest `pythonpath = ["src", "tests"]`.
  - `Makefile` exports `PYTHONPATH=src`.
  - The launchd plist sets `PYTHONPATH=<repo>/src`.
  - Docs tell you to `export PYTHONPATH=src`.
- Linux CI is unaffected.

### B-006: Baseline poll "took" 8 min 37 s with 18 `ReadError('')` / `ReadTimeout`
- **Symptom:** The first poll after seeding (576 boards) ran for 8.6 min. 18 boards failed with status 0.
- **Investigation:**
  - The same URLs took 0.15–0.55 s with curl, so it wasn't the APIs.
  - Per-minute request counts in `poller.log`: **504 requests at 18:39, then nothing until 18:47**,
    then a burst of errors at the same instant.
  - `pmset -g log`: `18:39:20 Entering Sleep state due to 'Maintenance Sleep' … 493 secs`, on AC power.
- **Cause:** **macOS idle sleep** in the middle of the poll. Sockets died while asleep, and on wake
  every pending request failed together.
- **Fix:** Long runs go under `caffeinate -i`; the launchd agent wraps the poller in it. Re-run: **649
  boards in 6.3 s, 0 errors**. The poller already tolerates errors (status 0 → retry at 2× the
  interval), so nothing was lost.

### B-007: H-1B seeding matched unrelated companies
- **Symptom:** `greenhouse/oath` (seed "Oath Holdings", i.e. Yahoo) listed veterinary jobs;
  `greenhouse/orchestra` (seed "Orchestra Technology") was a PR agency; `ashby/oath` was an unrelated
  startup.
- **Cause:** USCIS employer names have no website, so only name-based slugs exist, and short names
  collide.
- **Fix:**
  - `seeds.names_match`: Greenhouse returns the board's display name ("Oath Animal Hospital"), so a
    board is rejected when its name is a different, longer company. A short suffix is allowed, e.g.
    "BILL" vs "Bill Com".
  - Blocked the three boards.
  - "Orchestra" can't be caught by name, because the names genuinely match, so manual blocking stays
    the fallback.

### B-008: `hot_h1b_threshold` setting was unused
- **Symptom:** Found while documenting `config.yaml`: the setting existed (default 25), but the H-1B
  seeder hard-coded `hot_threshold=100`.
- **Fix:** The default is now 100 and `seed h1b` passes it through. The live config was updated to
  match.

### B-009: Agent's `run.log` stayed empty; `poller.log` would grow ~50 MB/day
- **Symptom:** Right after `agent install` the agent was polling (533 boards had a fresh
  `last_polled`), but `run.log` was still empty after 25 s. Meanwhile `poller.log` had one httpx INFO
  line per request, roughly 1,000 lines every 5 minutes.
- **Cause:** Under launchd, stdout is a file, so Python block-buffers it (8 KB). httpx logs every
  request at INFO. Separately, every all-304 cycle printed a line (one every 1–2 s).
- **Fix:** The plist sets `PYTHONUNBUFFERED=1`. The `httpx` logger is set to WARNING, and
  `poller.log` is a `RotatingFileHandler` (5 MB × 3). Cycle summaries are aggregated: printed every
  5 min, or immediately on a push, a baseline digest or an error.

### B-010: HN companies probed with a subdomain as their "domain root"
- **Symptom:** After the first HN baseline, `ashby/jobs` was registered for "CABLE" and
  `ashby/archive` for "Paradromics": unrelated boards.
- **Cause:** `slug_candidates` took the **first** host label as the domain root, so
  `jobs.cable.energy` → `jobs`. YC websites are `www.x.com`, so Phase 1 never hit it. Also the HN
  "website" guess picked the first non-ATS link, which for Paradromics was an `archive.ph` snapshot,
  and for others `jobs.gem.com` / `jobs.deel.com`. Those are hiring platforms, whose own boards would
  have been probed next.
- **Fix:**
  - `seeds.domain_root` returns the registrable name (`jobs.cable.energy` → `cable`,
    `acme.co.uk` → `acme`).
  - The HN site guess skips ~50 ATS, hiring-platform, social, archive and docs hosts.
- **Data repair:**
  - The thread was replayed offline. Boards that weren't linked from the post and weren't among the
    corrected candidates were deleted with their jobs: `ashby/jobs`, `ashby/archive`.
  - Those companies were re-probed.
  - A third board flagged by the replay, `greenhouse/octanelending`, was legitimate. It came from a
    `gh_jid` link, which only resolves over the network. It was restored with `applypilot resolve`.
  - Lesson: an offline replay of a resolver that has network steps must treat redirect-only links as
    "unknown", not "absent".

### B-011: Rubrik not found: "Rubrik Job Board" ≠ "Rubrik", and the miss was cached forever
- **Symptom:** `resolve --company Rubrik --title …` said "not found", yet
  `boards-api.greenhouse.io/v1/boards/rubrik` exists. The probe cache held `rubrik → []`.
- **Cause:**
  - Greenhouse's display name is "Rubrik Job Board". `names_match` allows at most 4 extra characters,
    so the board was rejected as a different company.
  - Probe results were cached permanently, including misses.
- **Fix:**
  - `names_match` strips generic board words ("job board", "careers (at)", "jobs (at)", "hiring",
    "open positions") before comparing.
  - `[]` results expire after 30 days, or after 1 day when `resolve_by_name` asks.
  - All 2,361 cached misses were cleared and the curated, YC and H-1B seeds re-run (1 min 37 s).
    Rubrik then resolved to `greenhouse/rubrik/jobs/8220615`.

### B-012: Re-seeding promoted placeholder boards out of cold
- **Symptom:** After the re-seed in B-011, `stats` showed 30 cold boards, down from 115 that morning,
  and 531 hot.
- **Cause:**
  - `add_board` upgrades a board to the seed's tier when that tier is higher.
  - The baseline's "no US jobs → cold" demotion (B-004) was therefore undone by re-adding the same
    company from curated, YC or H-1B.
  - Feed links (tier hot) could do the same.
- **Fix:**
  - A board that is cold **and** has a finished baseline stays cold on re-add. Only the poller's own
    promotion (a new relevant job) lifts it.
  - The test `test_reseeding_does_not_undo_cold_demotion` covers it.
- **Data repair:** The baseline rule was re-applied to boards with no post-baseline relevant job and
  no US-plausible open job. 118 were re-demoted (Figma, Reddit, Carta, Mercury… on Ashby: the B-004
  set). Result: 148 cold / 516 hot / 494 warm.

### B-013: Two listings of the same job could both push (caught in design review)
- **Symptom:** None in production; found while reviewing `FeedRunner._job_lead` before the first
  live run.
- **Cause:** The first draft checked `job_exists`, then awaited the Greenhouse description fetch, and
  only then inserted. Leads, and the two GitHub lists, are processed concurrently, so the same job
  listed in both lists could pass the check twice and push twice.
- **Fix:** Check, judge and insert run with no `await` between them. The description is fetched after
  the insert and written with `db.set_description`. The asyncio event loop is single-threaded, so
  that section is atomic.

### B-014: Hard drops missed on the labeled set (5 of 35)
- **Symptom:** The first run of the 73-excerpt eval: recall 30/35, 0 false drops.
- **Causes:**
  - "What We’re Looking For" used a curly apostrophe; the header regex expected `'`.
  - "Skills & Qualifications" and "You’ll Thrive Here If You" weren't recognized as requirement
    headers (the regex was anchored to the start of the line and lacked "thrive").
  - "U.S. citizenship and eligibility for a TS clearance (active TS strongly preferred)": the
    "preferred" inside the parentheses softened the whole clause.
- **Fix:** Normalize apostrophes; search requirement keywords anywhere in short header lines; ignore
  softeners inside parentheses. Result: 35/35, 0 false drops.

### B-015: Held-out miss: an ITAR clause read as "preferred"
- **Symptom:** A spot check of 30 kept jobs that mention citizenship, clearance or export control
  (not in the eval set) found one wrong keep. Northwood Space (Full Stack Developer): "under ITAR you
  must be a U.S. citizen, lawful permanent resident…" was flagged "citizenship preferred".
- **Cause:** The paragraph followed the "Preferred" list with no new header, so the section carried
  over.
- **Fix:** Must / only / limited to / is required in a clause outweighs an inherited `pref` section.
  "Additional information", "Please note", "Work authorization", "Legal" and "Location" headers end a
  section. The other 29 sampled keeps were right: Palantir "beneficial, but not necessary", OpenAI
  "may need to meet certain legal status requirements" (flagged), Cape listing citizenship under
  Preferred.

### B-016: "post-training feedback loops" dropped as a training-fee scam
- **Symptom:** The first backfill over 2,601 jobs hard-dropped OpenAI "AI Systems Engineer, Codex
  Agents" as an OPT training scheme.
- **Cause:** `training fee` matched inside "post-training **fee**dback"; the scam regex had no word
  boundaries.
- **Fix:** The scam alternatives are wrapped in `\b(...)\b`; regression case in
  `tests/test_filters.py`; the job was restored. The other scam drop (Arya Groups: "Stop the OPT
  Clock: Immediate enrollment…") is correct.

### B-017: Detection latency mixed board polls, feeds and re-listings
- **Symptom:** `stats` showed p50 29.8 min and p95 89 days (n=13) after the first feed day, against a
  5-minute goal.
- **Cause:** One number for everything. It mixed board-poll detections (0.4–2.5 min) with Simplify rows
  (26–30 min after the ATS posted, some added days late) and a Toast job first published 7.5 days
  earlier (re-listed).
- **Fix:** `stats` reports board polls and feeds separately and leaves out jobs first published
  > 48 h before detection (counted separately). Result: board polls p50 1.5 min, p95 2.5 min (n=5);
  feeds p50 29.8 min (n=6).

### B-018: Stored relevance verdicts predate newer title rules
- **Symptom:** "Specialist AI GTM Strategy" (Okta) topped the ranked queue although the title rules
  exclude GTM/strategy.
- **Cause:** `relevant` is decided once at insert; rules added later (GTM, today's clearance titles)
  never reached older rows.
- **Fix:** `applypilot screen` re-runs the title/location gate on every relevant open job first: 72
  demoted on 2026-10-21.

### B-019: Headless tailoring couldn't read the local repo copies
- **Symptom:** The first variant's report (ml-engineer) said "the sandbox blocked reads of
  `~/Documents/*`", so its repo sweep relied on the fact base only.
- **Cause:** `claude -p` runs with `cwd` = the resume folder, and reads outside the working directory
  need permission, which a headless session can't ask for.
- **Fix:** `tailor.repo_dirs` (set in the local `config.yaml`, not in code) lists the repo copies.
  Each existing one is passed as `--add-dir`. The next five variants read them (e.g. Portfolio's tests
  and WCAG work, OpsPilot's VPC/Jaeger Terraform and its post-incident report).

### B-020: A variant tailored to one posting generalized poorly
- **Symptom:** After the fit backfill, `ai-engineer` was the best variant for 1,079 of 1,745 jobs and
  `swe-backend` for only 111. On 40 plain "Software Engineer" postings, ai-engineer beat swe-backend
  36 times, by 7.6 points on average.
- **Cause:** A keyword diff over 60 SWE jobs showed it was content, not scorer noise. swe-backend was
  tuned to Notion's posting (95 on it) and lacked the generic terms other SWE postings use:
  production, software design, system design, observability, communication. ai-engineer had them.
- **Fix:** swe-backend was rebuilt against the role and requirement sections of four real new-grad
  SWE postings (Notion, Harvey, Cursor, Affirm). The full text, 19.7K chars, timed out at 30 min; the
  trimmed 10K chars took 5.8 min. It scores 94, and is now the best variant for 546 jobs (was 122).
  The Notion-only version is kept in `variants/_archive/`. `variants build --timeout` was added.

### B-021: Fit backfill took 7 min
- **Symptom:** `screen --all` with fit: 1,745 jobs in 7 min 13 s (~250 ms per job).
- **Cause:** `ats_score.score()` re-derives the JD's terms (about 200 regexes per line) for each of the
  six variants.
- **Fix:** `Fitter` caches `jd_terms` per JD, returning copies because `score()` mutates them.
  75 ms per job, with results identical to before on 100 of 100 jobs.

### B-022: A tailoring run hung 5.3 hours past its 30-minute timeout
- **Symptom:** The swe-backend rebuild started overnight and returned after 19,264 s with "API Error:
  Your computer went to sleep mid-response". It left a `.tex` newer than its PDF, scoring 76.
- **Cause:**
  - The Mac was on battery, and `caffeinate -i` only prevents *idle* sleep (B-006).
  - `asyncio.wait_for` measures the timeout on the monotonic clock, which doesn't advance while
    macOS sleeps, so the 1,800 s limit never expired in wall-clock terms.
- **Fix:**
  - `tailor.run` checks a **wall-clock** deadline (`time.time()`) every ≤ 30 s and kills the session
    when it passes; a test uses a 1 s limit.
  - The partial output was moved to `variants/_archive/` and the last good variant restored.
  - The earlier failure that night was the Claude usage limit ("You've hit your session limit"),
    returned cleanly as an error after 81 s.

### B-023: Wrong automatic answers found by reviewing 60 real forms
- **How found:** Every distinct automatic answer across the 60 highest-ranked jobs' forms was printed
  and read before any form was filled.
- **Wrong:**
  - "Have you worked at Snowflake … in any *capacity*" → "Morgantown, WV" (`city` matched inside
    "capacity").
  - Graduation dropdown → "Summer 2026" (any `2026` option; the truth is December).
  - "Are you able to relocate to NYC?" → "I am local to the NYC area" (any "I am…" counted as yes).
  - "How did you first learn about us?" → "Campus Career Fair" (`career` matched).
  - "Preferred First & Last Name" → the last name.
  - "West Virginia University" chosen in unrelated dropdowns mentioning "university".
  - "What is the address from which you plan on working?" → "Yes".
  - A Cape Town job's questions answered as if in the US.
  - Conditional follow-ups ("If you answered yes…") answered.
- **Fix:**
  - Rules reordered specific → general; word boundaries; yes/no options must say so outright.
  - School, location and link rules apply only to text fields.
  - Graduation options must name Oct–Dec 2026.
  - New always-yours classes: another country, follow-ups, two-part questions, "where will you work",
    "eligible to begin immediately" (F-1 OPT timing).
  - 34 regression tests use the real labels.

### B-024: Readback confirmed a wrong answer
- **Symptom:** Lever's "What is your preferred first name and last name?" was filled with the last name,
  and the readback said `filled`.
- **Cause:** The question is a multi-line card, and the name rules only applied to single-line fields.
  Readback compares the page with what was typed, so it can't detect a wrong rule.
- **Fix:** Name rules cover text areas; a regression test. Correctness rests on the answer review
  (B-023) and the tests; readback only proves the page holds the intended value.

### B-025: Ashby selectors
- **Symptom:** In a 12-form batch dry run, Ashby forms had 5 `failed` (`querySelectorAll … '#2…'`) and
  1 `mismatch` (location).
- **Cause:**
  - Ashby field ids are UUIDs that can start with a digit, which `#id` can't express.
  - Text search for "Location" matched the page's sidebar first.
  - Some location fields are plain text with no suggestions.
- **Fix:**
  - `[id="…"]` / `[name="…"]` attribute selectors.
  - Form `<label>` before plain text.
  - Location: pick only a suggestion naming the same city; with no suggestions, the typed text is the
    answer.
- **Result:** the same 12 forms gave 106 filled and verified, 0 mismatch, 0 failed. Databricks redirects
  to its own site and is reported instead.
