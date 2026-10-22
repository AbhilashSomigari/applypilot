# 5. Operations runbook

_Reflects: Phase 3a (2026-10-21)._

> **Always `export PYTHONPATH=src` first** (or use `make`). On this Mac, uv marks `.venv` hidden and
> Python 3.12.13 skips hidden `.pth` files, so the editable install is invisible (bug B-005).

## 5.1 First-time setup
```bash
cd ~/Documents/applypilot
uv sync
export PYTHONPATH=src
uv run applypilot init            # creates ~/.applypilot/{config.yaml, applypilot.db, profile.yaml, answers.yaml}
uv run applypilot test-push       # check phone (ntfy) and Mac notifications
```
**Phone:** install the ntfy app → subscribe to the topic shown by `init` (it's in
`~/.applypilot/config.yaml` as `ntfy_topic`). Keep the topic private.

## 5.2 Growing the board list
```bash
uv run applypilot seed curated                 # seeds/companies.txt → hot tier (~22 s for 132 companies)
uv run applypilot seed yc                      # actively hiring YC companies → warm (~76 s)
uv run applypilot seed h1b --min-approvals 25  # USCIS FY2023 employers → warm, ≥ 100 approvals → hot (~49 s)
uv run applypilot boards add greenhouse <slug> --company "Name" --tier hot
uv run applypilot boards list --tier hot --limit 100
uv run applypilot boards block lever neon      # wrong company sharing a slug; permanent, deletes its jobs
```
To add a company to the curated list, append `Name, website` to `seeds/companies.txt` and rerun
`seed curated`. Already-probed companies come from the cache ("no board" results are retried after
30 days).

The feeds (§5.3a) also add boards on their own: every Greenhouse/Lever/Ashby link in a new-grad list
row or an HN post is registered, and companies without one are probed.

## 5.3 Polling
```bash
caffeinate -i uv run applypilot poll           # one cycle; the first cycle of a new board is the silent baseline
uv run applypilot poll --no-push               # same, without notifications
uv run applypilot run                          # boards + feeds forever in the foreground (Ctrl-C to stop)
uv run applypilot run --no-feeds               # boards only
uv run applypilot jobs --hours 24              # relevant jobs DETECTED in the last 24 h
uv run applypilot jobs --fresh --hours 48      # relevant jobs POSTED in the last 48 h (includes first scans)
uv run applypilot stats                        # boards per tier/ATS/source, open/relevant jobs, latency p50/p95 (7 d)
```
`jobs` has a `via` column: `ats` (board poll) or the feed that found the job first.

## 5.3a Feeds and the resolver
```bash
caffeinate -i uv run applypilot feeds poll --force   # fetch every feed now (the first fetch is a silent baseline, ~2 min)
uv run applypilot feeds poll --no-push               # only due feeds, no notifications
uv run applypilot feeds list                         # baseline state, last poll/status, leads and jobs per feed
uv run applypilot resolve "https://careers.aqr.com/jobs?gh_jid=8284812"     # any job link → ATS job (adds the board)
uv run applypilot resolve --company "Rubrik" --title "Channel/Partner Sales Engineer - Southeast" --location "Atlanta, GA"
```
### Google Jobs (JSearch)
1. Get a key: **app.openwebninja.com** (direct; use `--provider openwebninja`), or the JSearch API on RapidAPI (default provider). If rapidapi.com is down, use the direct one.
2. Run this **in your own terminal**, because the key is typed hidden:
   ```bash
   uv run applypilot feeds set-key jsearch --monthly-requests 200      # 10000 on Pro; --provider openwebninja for a direct key
   uv run applypilot feeds test jsearch                                # one request: shows results and the most direct link of each
   uv run applypilot feeds list                                        # shows each query's pace vs the quota
   ```
3. `set-key` restarts the agent if it's installed. Each query's first fetch is a silent baseline.
4. Edit `feeds.jsearch.queries` in `config.yaml` to change what's searched. More queries mean each one
   runs less often on the same quota. Then run `agent install`.
5. `feeds poll --force` and `feeds test` use quota outside the pacing. On the free plan, use them
   sparingly.

`resolve --company --title` is the LinkedIn/Indeed path: paste the company and title from a listing
and get the employer's own apply link. An untracked company is probed, its board scanned once
(silently) and matched. Feeds and their intervals are configured under `feeds:` in `config.yaml`.

### Gmail alerts (read-only sign-in)
One-time Google Cloud setup, signed in as the job-search Gmail account:
1. Create a project, then enable the **Gmail API** (APIs & Services → Library).
2. **Google Auth Platform** (`console.cloud.google.com/auth/overview`) → Get started: app name, support
   email, audience **External**, contact email.
3. **Data access** → add the scope `.../auth/gmail.readonly`.
4. **Audience**:
   - **Testing** (current): add your own Gmail under **Test users**. Google revokes the sign-in
     **7 days** after each login, so run `gmail login` again weekly.
   - **In production**: no expiry, but Branding must have a homepage URL and a privacy-policy URL
     (with its domain under Authorized domains). No Google verification is needed for personal use;
     the first sign-in shows an "unverified app" warning once (Advanced → Go to ApplyPilot).
5. **Clients** → Create client → **Desktop app** → Download JSON, then:
   ```bash
   mv ~/Downloads/client_secret_*.json ~/.applypilot/gmail_client_secret.json
   chmod 600 ~/.applypilot/gmail_client_secret.json
   ```
Then, **in your own terminal** (it opens the browser):
```bash
uv run applypilot gmail login       # browser consent; saves ~/.applypilot/gmail_token.json (mode 600)
uv run applypilot gmail status      # account, sign-in age, alert emails per site in the last 14 days
uv run applypilot gmail test --days 3   # parse recent alert emails: cards + relevance (stores nothing, opens no links)
uv run applypilot gmail samples     # newest 2 alert emails per site → ~/.applypilot/samples/*.eml (mode 600)
uv run applypilot gmail logout      # revokes the sign-in at Google and deletes the token
```
`gmail login` restarts the agent, which then reads alert emails every 2 min (first read: a silent
3-day baseline). In Testing mode the phone gets "Gmail sign-in ends tomorrow" on day 6 and "expired"
if it lapses; run `gmail login` again. Indeed alerts are created on indeed.com (Job alerts); check
the search terms are spelled right.

Never paste the client JSON, the token, or sample emails into chat or the repo. Scrub names, email
addresses and tracking links before a sample becomes a test fixture.

## 5.3b Screening and the ranked queue
```bash
uv run applypilot screen                   # re-check titles, then screen unscreened relevant jobs with a description
uv run applypilot screen --fetch-days 14   # first fetch missing Greenhouse/Lever/Ashby descriptions (posted ≤ 14 d)
uv run applypilot screen --all             # re-screen everything (after changing filters.py)
uv run applypilot jobs --hours 72 --fresh --by-score   # the queue: highest score first, with visa signals
uv run applypilot stats                    # screened / hard-dropped counts; latency by board polls vs feeds
```
New jobs are screened automatically before the push. After editing `filters.py`, run
`uv run pytest tests/test_filters.py` (the 73-excerpt labeled set must stay at 100% recall and 0 false
drops), then `screen --all` and `agent install`.

## 5.3c Resume variants and tailoring
```bash
uv run applypilot variants list                          # archetype JD, score and PDF per variant
caffeinate -i uv run applypilot variants build           # all 6 (2 at a time; ~10-25 min each; uses Claude usage)
caffeinate -i uv run applypilot variants build --only ml-engineer --timeout 3600   # long archetypes need more time
uv run applypilot screen --all                           # re-score every job with fit, once variants exist
uv run applypilot tailor <job id>                        # a resume for one job → Resume/tailored/<company>-<title>/
```
The archetype JDs are real postings saved in `Resume/variants/<name>/jd.txt`; replace one to retarget
that variant, then rebuild it. A variant meant for a whole family works better from **several**
postings' role and requirement sections (swe-backend uses four; see B-020). Keep it near 10K characters.
Build on AC power with the lid open: lid-closed battery sleep kills the session (B-022). Each run's `report.md` lists **unconfirmed tools**: the batch run never
adds them. Confirm them in an interactive `/resumetailor` session (which records them in the resume
`CLAUDE.md`), then rebuild.

## 5.3d Applying (Phase 4a: plan and dry run; nothing is submitted)
```bash
uv run applypilot plan <job id>                  # every field → answer and source, the resume, what needs you
uv run applypilot fill <job id>                  # dry run in a visible Chrome: fill, read back, screenshot, close
uv run applypilot fill <job id> --keep-open      # leave the window open to inspect (press Enter to close)
uv run applypilot fill <job id> --with-resume    # also attach the resume (Lever/Ashby upload it right away)
uv run pytest -m browser                         # the filler's offline browser tests (~40 s)
```
Fill in the empty `misc.*` keys in `~/.applypilot/answers.yaml` to make those questions automatic.

### Applying for real (Phase 4b): run in your own Terminal, since it waits for your typing
```bash
cd ~/Documents/applypilot && export PYTHONPATH=src
uv run applypilot queue                     # next best jobs, numbered: score, posted, resume variant, job id
uv run applypilot plan 3                    # optional preview of row 3 (or pass the full job id)
uv run applypilot apply 3                   # fills Chrome; you complete the red-outlined fields; type 'submit'
uv run applypilot applications              # the log
```
After a confirmed submission, `apply` offers to remember the answers you typed (never legal notices
or essays):
```bash
uv run applypilot draft 3            # preview drafted written answers and their grounding checks
uv run applypilot prepare            # prepare the next 3 high-scoring jobs now (the agent does it every 5 min)
uv run applypilot digest             # today's digest now (--push to send it to the phone)
uv run applypilot responses          # scan Gmail for replies; outcome per application
uv run applypilot approvals status   # phone approval: active? supervised count, offers today, recent approvals
uv run applypilot approvals enable   # turn on one-tap Apply pushes (after 5 supervised applications)
uv run applypilot approvals disable
uv run applypilot memory list        # what's remembered, and for which company
uv run applypilot memory forget 4    # delete entry 4 (or edit ~/.applypilot/answer_memory.yaml)
```
Row numbers refer to the last `queue` you ran (saved in `~/.applypilot/last_queue.json`). The job id
column wraps instead of truncating in narrow terminals.
Submit happens only when you type `submit`; `skip` cancels. Solve any CAPTCHA in the window. A form
that shows errors after Submit waits for you to fix them. Screenshots go to
`~/.applypilot/applications/`.

## 5.3e Dashboard
```bash
open http://127.0.0.1:8787              # served by the launchd agent com.applypilot.web (log ~/.applypilot/web.log)
uv run applypilot web --port 8799       # or run it in the foreground on another port
```
`agent install` installs and reloads both agents (poller and dashboard); `agent status` shows both.

## 5.4 Background agent (launchd)
```bash
uv run applypilot agent install     # writes ~/Library/LaunchAgents/com.applypilot.poller.plist and loads it
uv run applypilot agent status      # "state = running pid = …" or "not installed"
uv run applypilot agent uninstall
tail -f ~/.applypilot/run.log       # agent stdout: pushes, digests, errors, a summary every 5 min
tail -f ~/.applypilot/poller.log    # warnings (rotates at 5 MB × 3)
pmset -g assertions | grep caffeinate   # confirm the agent is holding off idle sleep
```
The plist runs `caffeinate -i uv run --project <repo> applypilot run` (boards + feeds) with
`PYTHONPATH=<repo>/src`, `PYTHONUNBUFFERED=1`, `RunAtLoad`, `KeepAlive` and `ThrottleInterval 30`.
**After changing code, rerun `agent install`**: it reloads the agent, which otherwise keeps running
the old code. `caffeinate -i` prevents **idle** sleep. Closing
the lid on battery still sleeps the Mac, and polling resumes on wake.

## 5.5 Useful SQL
```bash
sqlite3 ~/.applypilot/applypilot.db
```
```sql
-- boards by tier and source
SELECT source, tier, COUNT(*) FROM boards GROUP BY 1,2;
-- sanity-check a board really belongs to the company (sample titles/locations)
SELECT title, location FROM jobs WHERE ats='ashby' AND slug='notion' ORDER BY random() LIMIT 10;
-- failing boards
SELECT ats, slug, last_status, fail_count FROM boards WHERE fail_count > 0;
-- new (post-baseline) relevant jobs and their detection latency in minutes
SELECT company, title, (detected_at - posted_at)/60.0 AS mins FROM jobs
 WHERE is_baseline=0 AND relevant=1 ORDER BY detected_at DESC LIMIT 20;
-- force every board to be due now
UPDATE boards SET next_poll=0 WHERE tier NOT IN ('dead','blocked');
-- what each feed has contributed
SELECT source, COUNT(*) FROM boards GROUP BY 1;
SELECT source, COUNT(*) FROM jobs WHERE relevant=1 AND closed_at IS NULL GROUP BY 1;
-- jobs found on ATSs we don't poll (Workday, SmartRecruiters, ...)
SELECT ats, COUNT(*) FROM jobs WHERE ats NOT IN ('greenhouse','lever','ashby') GROUP BY 1;
-- replay a feed from scratch (next fetch is a new silent baseline)
DELETE FROM feed_items WHERE feed='hn'; UPDATE feeds SET baseline_done=0, cursor=NULL, etag=NULL WHERE name='hn';
-- re-probe a company
DELETE FROM probes WHERE company_key='figureai';
```

## 5.6 Tests and lint
```bash
make test    # or: PYTHONPATH=src uv run pytest -q
make lint
```

## 5.7 Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'applypilot'` | Hidden `.pth` files (B-005) | `export PYTHONPATH=src`, or use `make` |
| A poll "takes" minutes, then many `ReadError('')` / `ReadTimeout` | The Mac idle-slept mid-poll (B-006). Check `pmset -g log \| grep Sleep` | Run under `caffeinate -i`; the agent already does |
| Alerts for a company you didn't expect (e.g. a vet clinic) | Slug collision (B-003, B-007) | `boards block <ats> <slug>` |
| A board shows 0 jobs and sits in cold | Empty placeholder board on that ATS (B-004) | Nothing; the real board is on another ATS |
| No phone alerts | Topic not subscribed, or `ntfy_topic` empty | `test-push`; check `config.yaml` |
| Nothing new after the first poll | Expected: the first scan is a silent baseline | `jobs --fresh` shows recent baseline jobs |
| `run.log` empty while the agent runs | Old plist without `PYTHONUNBUFFERED` (B-009), or nothing happened in the last 5 min | `agent install` again; wait for the 5-min summary |
| A feed-added Ashby board keeps 404ing | The company left Ashby or disabled its public board, but the list row is stale | Nothing: 3 × 404 → dead |
| `resolve --company` says "not found" | Company not on Greenhouse/Lever/Ashby, or the title differs a lot | Try the exact ATS title; Workday companies are Phase 5 |
| JSearch feed shows status 401/403 | Wrong key, or no subscription to the API on RapidAPI | `feeds set-key jsearch` again; on RapidAPI, subscribe to the (free) plan |
| JSearch status 429 | Monthly quota or rate limit hit | Interval doubles automatically; lower `queries` or raise `monthly_requests` only if the plan allows |
| Code change has no effect in the background | The agent still runs the old code | `uv run applypilot agent install` |
