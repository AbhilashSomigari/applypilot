# ApplyPilot documentation

These documents describe the whole tool: why it exists, how it's built, the data schema, how to run it,
what broke along the way, and what comes next. Update them whenever behavior changes. Every file
says which phase it reflects.

| # | Document | What's in it |
|---|---|---|
| 0 | [PROGRESS.md](PROGRESS.md) | Dated changelog and the current status of each phase |
| 1 | [01-idea-and-goals.md](01-idea-and-goals.md) | The problem, the F-1/OPT constraints, inspirations, decisions, ground rules |
| 2 | [02-architecture-and-workflow.md](02-architecture-and-workflow.md) | Components, the end-to-end pipeline, the poll lifecycle, module map |
| 3 | [03-tech-stack-and-tools.md](03-tech-stack-and-tools.md) | Libraries and why each was chosen, external data sources and APIs, dev tools |
| 4 | [04-data-schema.md](04-data-schema.md) | SQLite tables and columns, state machines, in-memory models, config and profile files |
| 5 | [05-operations-runbook.md](05-operations-runbook.md) | Every CLI command, setup, background agent, troubleshooting |
| 6 | [06-bugs-and-fixes.md](06-bugs-and-fixes.md) | Every bug and error found so far: symptom, root cause, fix |
| 7 | [07-roadmap-and-improvements.md](07-roadmap-and-improvements.md) | Phases, known limitations, improvement backlog |

Quick orientation: ApplyPilot polls public company job boards (Greenhouse, Lever, Ashby) every few
minutes, detects postings the moment they appear, filters them for an early-career F-1 candidate, and
pushes an alert to the phone. Cross-employer feeds (GitHub new-grad lists, HN) cover the ATSs it can't
poll and keep adding company boards, through a URL → ATS resolver. Later phases add more sources, resume tailoring and one-tap approved
applications.
