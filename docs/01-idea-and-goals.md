# 1. Idea and goals

_Reflects: Phase 2, part 1 (2026-10-20)._

## The problem
Recruiters often review the first ~100 applications to a posting. By the time a job shows up on
LinkedIn or Indeed, it has usually been live on the company's own applicant-tracking system (ATS) for
hours. Early-career candidates who apply late compete with hundreds of others.

The user is an **F-1 international M.S. CS student** (graduating Dec 2026):
- **OPT (12 months) + STEM OPT extension (24 months) ≈ 3 years** of work authorization without
  sponsorship, so "no sponsorship" jobs are **in scope**.
- **Out of scope** are roles he legally can't take: U.S. citizenship or green card required, security
  clearance, ITAR/EAR "U.S. person" roles.
- STEM OPT years 2–3 require an **E-Verify-enrolled employer**. That's a ranking signal, not a filter.
- Forms that ask "Will you require sponsorship now or in the future?" get a truthful "Yes (future)",
  which may auto-reject. The tool applies anyway and flags the risk.

## The goal
**Find relevant jobs faster than anyone else, on legitimate sources, and apply at high volume with
one-tap approval.**

| Target | Value |
|---|---|
| Posting → phone alert | median < 5 min |
| Posting → application ready to approve | < 10 min |
| Throughput | 100+ applications/day at ~10 s of review each |
| Accuracy | 0 wrong screening answers; never drop an OPT-eligible job |

## Inspirations, and what we took or rejected
| Product | Model | Taken | Rejected |
|---|---|---|---|
| **Tsenta** (YC) | Monitors 50K career pages; auto-fills Workday, Greenhouse, Lever, Ashby; tracks status from the inbox | ATS-direct monitoring = being first; diff shown before sending; inbox status tracking | Volume for its own sake |
| **ApplyRyt** | Humans apply for you on career pages only, "no Easy Apply"; $249–299/mo | Career pages only; tailored resume per job | Paying a third party; no control over answers |
| **LazyApply** | Fully automatic mass-apply, up to 1,500/day, including Indeed and ZipRecruiter | The volume target | No review step; bot use on boards that ban it |

## Decisions (from the user, 2026-10-20)
1. **Approval:** he approves every application. Approval is one tap, then the tool submits.
2. **Work-authorization answers** are fixed in `answers.yaml`: authorized Yes; sponsorship now No;
   sponsorship in the future Yes.
3. **Runtime:** local on his Mac, using Chrome.
4. **Don't drop no-sponsorship jobs** (3 years of OPT/STEM OPT).
5. **Every source** (career pages, LinkedIn, Indeed, YC, …) and **maximum speed and volume**.

## Ground rules
- **Legitimate sources only.**
  - Public, keyless job-board APIs and employer career pages.
  - Official government data (USCIS, DOL, E-Verify).
  - Public community data: GitHub new-grad lists (their published `listings.json`) and HN "Who is
    hiring" through the official Algolia API.
  - LinkedIn, Indeed, Handshake and similar sites through **their own job-alert emails** (Phase 2),
    never scraping. LinkedIn bans automated accounts.
- **Apply on the employer's ATS**, not Easy Apply.
- **Never bypass CAPTCHAs.** They're handed to the user.
- **Truthful answers only**, from verified facts (`~/Documents/Resume/CLAUDE.md`).
- **Politeness:** conditional requests, per-host concurrency limits, jittered intervals, descriptive
  User-Agent.
- **Per-company cap** (Phase 4): ≤ 3 applications per company per 30 days, so we never look like spam.
