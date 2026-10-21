"""Tiered, conditional polling of ATS boards.

Each board is polled on its tier's interval (hot ~2.5 min, warm 15 min, cold 3 h) with
If-None-Match, so an unchanged board costs one 304. A board's first successful scan is the
*baseline*: its jobs are recorded but not pushed one by one (only a digest of the fresh ones),
so adding 1,000 boards does not produce 30,000 alerts. Any job id that appears after the
baseline is new, gets judged, and relevant ones are pushed immediately.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from applypilot import db, notify
from applypilot.config import USER_AGENT, Settings
from applypilot.models import ATS, Board, BoardResult, Job
from applypilot.relevance import is_us, judge
from applypilot.sources import SOURCES

log = logging.getLogger("applypilot.poller")


@dataclass
class PollStats:
    polled: int = 0
    unchanged: int = 0
    errors: int = 0
    new_jobs: int = 0
    relevant: list[tuple[Job, str]] = field(default_factory=list)
    fresh_baseline: list[tuple[Job, str]] = field(default_factory=list)
    latencies_s: list[float] = field(default_factory=list)  # detected - posted, for new relevant jobs

    def merge(self, other: PollStats) -> None:
        self.polled += other.polled
        self.unchanged += other.unchanged
        self.errors += other.errors
        self.new_jobs += other.new_jobs
        self.relevant += other.relevant
        self.fresh_baseline += other.fresh_baseline
        self.latencies_s += other.latencies_s


def make_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.timeout_s,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=settings.max_concurrency, max_keepalive_connections=32),
        http2=False,
    )


class Poller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        push: bool = True,
        sources=SOURCES,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.client = client
        self.push = push
        self.sources = sources
        self.global_sem = asyncio.Semaphore(settings.max_concurrency)
        self.host_sem: dict[ATS, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.per_host_concurrency)
        )

    def _interval(self, tier: str, ok: bool) -> float:
        base = getattr(self.settings.tiers, tier, self.settings.tiers.warm)
        # jitter spreads boards out so polls don't arrive in bursts; errors back off
        return base * random.uniform(0.85, 1.15) * (1 if ok else 2)

    async def poll_board(self, board: Board) -> PollStats:
        stats = PollStats(polled=1)
        source = self.sources[board.ats]
        async with self.global_sem, self.host_sem[board.ats]:
            try:
                result = await source.poll(self.client, board)
            except (httpx.HTTPError, ValueError) as e:
                result = BoardResult(status=0, error=repr(e))
        now = time.time()
        if result.status == 304:
            stats.unchanged = 1
        elif result.status != 200:
            stats.errors = 1
            log.warning("poll %s/%s -> %s %s", board.ats, board.slug, result.status, result.error or "")
        else:
            await self._diff(board, result, now, stats)
        db.record_poll(self.conn, board, result.status, result.etag, now,
                       now + self._interval(board.tier, result.status in (200, 304)))
        return stats

    async def _diff(self, board: Board, result: BoardResult, now: float, stats: PollStats) -> None:
        current = {j.ext_id: j for j in result.jobs}
        known = db.board_ids(self.conn, board.ats, board.slug)
        new = [j for e, j in current.items() if e not in known]
        reopened = [e for e in current if known.get(e) is False]
        gone = [e for e, is_open in known.items() if is_open and e not in current]
        db.reopen_jobs(self.conn, board.ats, board.slug, reopened)
        db.close_jobs(self.conn, board.ats, board.slug, gone, now)
        baseline = not board.baseline_done
        if baseline and board.tier != "cold" and not any(is_us(j) is not False for j in result.jobs):
            # empty placeholder boards and non-US companies sharing a slug: keep, but poll rarely
            db.set_tier(self.conn, board.ats, board.slug, "cold")
            board.tier = "cold"
        if not new:
            return
        verdicts = {j.ext_id: judge(j) for j in new}
        relevant = [j for j in new if verdicts[j.ext_id].relevant]
        # descriptions only for jobs worth reading (Greenhouse lists omit them)
        if relevant and not baseline:
            source = self.sources[board.ats]
            async with self.host_sem[board.ats]:
                await asyncio.gather(*(source.details(self.client, j) for j in relevant), return_exceptions=True)
        db.insert_jobs(self.conn, new, now, baseline)
        for j in new:
            v = verdicts[j.ext_id]
            db.set_relevance(self.conn, j.id, v.relevant, v.reason)
        stats.new_jobs = len(new)
        fresh_cutoff = now - self.settings.fresh_hours * 3600
        for j in relevant:
            reason = verdicts[j.ext_id].reason
            if baseline:
                if j.posted_at and j.posted_at >= fresh_cutoff:
                    stats.fresh_baseline.append((j, reason))
                continue
            stats.relevant.append((j, reason))
            if j.posted_at:
                stats.latencies_s.append(max(0.0, now - j.posted_at))
        if stats.relevant and board.tier != "hot":
            # boards that post early-career roles are where the next one will appear
            db.set_tier(self.conn, board.ats, board.slug, "hot")
        if stats.relevant and self.push:
            for j, reason in stats.relevant:
                await notify.push(self.client, self.settings, j, reason)
            db.mark_notified(self.conn, [j.id for j, _ in stats.relevant], time.time())

    async def run_once(self, limit: int = 2000) -> PollStats:
        boards = db.due_boards(self.conn, time.time(), limit)
        total = PollStats()
        for s in await asyncio.gather(*(self.poll_board(b) for b in boards)):
            total.merge(s)
        return total

    async def run_forever(self, on_cycle=None) -> None:
        while True:
            stats = await self.run_once()
            if on_cycle and stats.polled:
                on_cycle(stats)
            nxt = db.next_due(self.conn)
            wait = 30.0 if nxt is None else min(30.0, max(1.0, nxt - time.time()))
            await asyncio.sleep(wait)
