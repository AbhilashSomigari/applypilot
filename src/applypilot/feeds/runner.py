"""Poll feeds and turn their leads into jobs and boards.

Job leads (a GitHub list row, a Google Jobs result) resolve to one ATS job: it is stored under the id
the board poller would use, judged by the same relevance gate, and pushed if relevant and not already
known. When every link is an aggregator (LinkedIn, Indeed, ...), the job is looked up on the company's
ATS board by company + title instead. A known job that was only seen on a silent board baseline, is
fresh, and was never pushed is pushed now: the feed just told us it matters. Company
leads (an HN post) only add boards. Either way every pollable board a link names is registered,
and companies without one are probed, so the next posting is caught on the ATS itself.

A feed's first successful fetch is its baseline, like a board's: leads are recorded, and nothing
is pushed (fresh relevant jobs go to the digest).
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field

import httpx

from applypilot import db, gmail, notify, rank, resolver
from applypilot.config import Settings
from applypilot.feeds.base import Feed, Lead
from applypilot.feeds.email_alerts import PARSERS, EmailAlerts
from applypilot.feeds.github_lists import GitHubList
from applypilot.feeds.hn import HackerNews
from applypilot.feeds.jsearch import JSearch
from applypilot.models import Job
from applypilot.poller import Poller
from applypilot.relevance import Verdict, judge
from applypilot.seeds import Seed, add_companies, company_key
from applypilot.sources import SOURCES

log = logging.getLogger("applypilot.feeds")


def default_feeds(settings: Settings) -> list[Feed]:
    f = settings.feeds
    feeds: list[Feed] = [GitHubList(repo, f.github_interval) for repo in f.github_lists]
    if f.hn:
        feeds.append(HackerNews(f.hn_interval))
    if f.jsearch.api_key:
        feeds += [JSearch(q, f.jsearch) for q in f.jsearch.queries]
    if f.gmail and gmail.token_info():
        feeds += [EmailAlerts(site, settings) for site in PARSERS]
    return feeds


@dataclass
class FeedStats:
    feeds: int = 0
    unchanged: int = 0
    errors: int = 0
    leads: int = 0  # unseen leads processed
    new_jobs: int = 0
    known_jobs: int = 0  # already found by the board poller (or another feed)
    resolved_by_name: int = 0  # aggregator-only leads matched to an ATS job by company + title
    new_boards: int = 0
    probed: int = 0
    relevant: list[tuple[Job, str]] = field(default_factory=list)
    fresh_baseline: list[tuple[Job, str]] = field(default_factory=list)

    def merge(self, o: FeedStats) -> None:
        for k in ("feeds", "unchanged", "errors", "leads", "new_jobs", "known_jobs", "resolved_by_name",
                  "new_boards", "probed"):
            setattr(self, k, getattr(self, k) + getattr(o, k))
        self.relevant += o.relevant
        self.fresh_baseline += o.fresh_baseline


class FeedRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        push: bool = True,
        feeds: list[Feed] | None = None,
        concurrency: int = 6,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.client = client
        self.push = push
        self.feeds = feeds if feeds is not None else default_feeds(settings)
        # below the client's pool size, so a feed baseline (thousands of probes) never starves board polls
        self.sem = asyncio.Semaphore(concurrency)
        self._probed: set[str] = set()
        self._poller: Poller | None = None  # scans boards that resolve_by_name just added

    async def run_feed(self, feed: Feed) -> FeedStats:
        stats = FeedStats(feeds=1)
        state = db.feed_state(self.conn, feed.name)
        try:
            result = await feed.fetch(self.client, state["etag"], state["cursor"])
        except (httpx.HTTPError, ValueError, LookupError) as e:
            result = None
            log.warning("feed %s -> %r", feed.name, e)
        now = time.time()
        status = result.status if result else 0
        if status == 304:
            stats.unchanged = 1
        elif status != 200:
            stats.errors = 1
            if result:
                log.warning("feed %s -> %s %s", feed.name, status, result.error or "")
        else:
            baseline = not state["baseline_done"]
            unseen = db.unseen_items(self.conn, feed.name, [lead.item_id for lead in result.leads])
            todo = [lead for lead in result.leads if lead.item_id in unseen]
            done = await asyncio.gather(*(self._lead(feed, lead, baseline, stats) for lead in todo))
            db.mark_items(self.conn, feed.name, [(lead.item_id, j) for lead, j in zip(todo, done, strict=True)], now)
            stats.leads = len(todo)
        ok = status in (200, 304)
        nxt = now + feed.interval_s * random.uniform(0.9, 1.1) * (1 if ok else 2)
        db.record_feed(self.conn, feed.name, status, result.etag if result else state["etag"],
                       result.cursor if result else None, now, nxt)
        return stats

    async def _lead(self, feed: Feed, lead: Lead, baseline: bool, stats: FeedStats) -> str | None:
        """Process one lead; returns the job id it resolved to (job leads only)."""
        async with self.sem:
            try:
                if feed.jobs:
                    return await self._job_lead(feed, lead, baseline, stats)
                await self._company_lead(feed, lead, stats)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("feed %s lead %s (%s) -> %r", feed.name, lead.item_id, lead.company, e)
        return None

    async def _company_lead(self, feed: Feed, lead: Lead, stats: FeedStats) -> None:
        linked_board = False
        for url in lead.urls:
            link = await resolver.resolve(self.conn, self.client, url, lead.company, feed.name, tier="warm")
            if link and link.pollable:
                linked_board = True
                stats.new_boards += link.new_board
        if not linked_board and lead.company:
            await self._probe(lead.company, lead.website, feed.name, stats)

    async def _probe(self, company: str, website: str | None, source: str, stats: FeedStats) -> None:
        key = company_key(company)
        if not key or key in self._probed:  # one list often has dozens of rows per company
            return
        self._probed.add(key)
        # probed slugs are guesses (Lever/Ashby can't be name-checked), so they start warm, not hot
        c = await add_companies(self.conn, self.client, [Seed(company, website, "warm", source)], concurrency=2)
        stats.probed += c["probed"]
        stats.new_boards += c["new_boards"]

    async def _job_lead(self, feed: Feed, lead: Lead, baseline: bool, stats: FeedStats) -> str | None:
        if not lead.urls:
            return None
        url = resolver.best_url(lead.urls)
        if url is None and lead.title and lead.company:
            # only aggregator links: find the employer's own posting (never fetches LinkedIn/Indeed)
            if self._poller is None:
                self._poller = Poller(self.conn, self.settings, self.client, push=False)
            row = await resolver.resolve_by_name(
                self.conn, self.client, self._poller, lead.company, lead.title, lead.location, feed.name)
            if row is not None:
                stats.resolved_by_name += 1
                return await self._known(feed, row, baseline, stats)
        # nothing better: keep the aggregator link (manual apply) so the job is still seen and pushed
        link = await resolver.resolve(self.conn, self.client, url or lead.urls[0], lead.company, feed.name, tier="hot")
        if link is None or not link.job_id:
            return None
        stats.new_boards += link.new_board
        job_id = await self._record(feed, lead, link, baseline, stats)
        if not link.pollable and lead.company:
            # after the push: a Workday listing's company may still run a Greenhouse/Lever/Ashby board
            await self._probe(lead.company, None, feed.name, stats)
        return job_id

    async def _record(self, feed: Feed, lead: Lead, link: resolver.Link, baseline: bool, stats: FeedStats) -> str:
        job = Job(
            ats=link.ats, slug=link.slug, ext_id=link.ext_id, title=lead.title, company=lead.company or link.slug,
            location=lead.location, country=lead.country, remote="remote" in lead.location.lower(), url=link.url,
            apply_url=link.apply_url, description=lead.description, posted_at=lead.posted_at, source=feed.name,
            raw=lead.raw,
        )
        # check and insert with no await in between, so two listings of one job can't both push
        if (row := db.get_job(self.conn, job.id)) is not None:
            return await self._known(feed, row, baseline, stats)
        v = Verdict(False, lead.drop_reason) if lead.drop_reason else judge(job)
        now = time.time()
        db.insert_jobs(self.conn, [job], now, baseline)
        db.set_relevance(self.conn, job.id, v.relevant, v.reason)
        stats.new_jobs += 1
        if not v.relevant:
            return job.id
        if link.pollable and not baseline and not job.description:
            try:
                await SOURCES[link.ats].details(self.client, job)
                db.set_description(self.conn, job.id, job.description)
            except (httpx.HTTPError, ValueError):
                pass
        keep, signals = rank.screen(self.conn, job, now, self.settings)
        if not keep:
            return job.id  # hard drop from the description (citizenship, clearance, ITAR, scam, 4+ years)
        reason = f"{v.reason}{f' · {signals}' if signals else ''} · via {feed.label}"
        if baseline:
            if job.posted_at and job.posted_at >= now - self.settings.fresh_hours * 3600:
                stats.fresh_baseline.append((job, reason))
            return job.id
        stats.relevant.append((job, reason))
        if self.push:
            await notify.push(self.client, self.settings, job, reason)
            db.mark_notified(self.conn, [job.id], time.time())
        return job.id

    async def _known(self, feed: Feed, row: sqlite3.Row, baseline: bool, stats: FeedStats) -> str:
        """A lead for a job already stored (by a board poll or another feed)."""
        stats.known_jobs += 1
        fresh = row["posted_at"] and row["posted_at"] >= time.time() - self.settings.fresh_hours * 3600
        if baseline or not row["relevant"] or row["notified_at"] is not None or not fresh:
            return row["id"]
        # only seen on a silent baseline so far: push it now (marked first, so no second lead re-pushes)
        db.mark_notified(self.conn, [row["id"]], time.time())
        job = db.row_job(row)
        if not job.description and job.ats in SOURCES:
            try:  # Greenhouse baseline jobs were stored without one
                await SOURCES[job.ats].details(self.client, job)
                db.set_description(self.conn, job.id, job.description)
            except (httpx.HTTPError, ValueError):
                pass
        keep, signals = rank.screen(self.conn, job, settings=self.settings)
        if not keep:
            return row["id"]
        reason = f"{row['reason']}{f' · {signals}' if signals else ''} · via {feed.label}"
        stats.relevant.append((job, reason))
        if self.push:
            await notify.push(self.client, self.settings, job, reason)
        return row["id"]

    async def run_once(self, force: bool = False) -> FeedStats:
        now = time.time()
        due = [f for f in self.feeds if force or db.feed_state(self.conn, f.name)["next_poll"] <= now]
        total = FeedStats()
        for s in await asyncio.gather(*(self.run_feed(f) for f in due)):
            total.merge(s)
        return total

    async def run_forever(self, on_cycle=None) -> None:
        while True:
            stats = await self.run_once()
            if on_cycle and stats.feeds:
                on_cycle(stats)
            await asyncio.sleep(15)
