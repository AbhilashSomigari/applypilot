"""Grow the board list: turn company names (YC, USCIS H-1B data, a curated list) into ATS boards.

A company's board slug is usually its name or domain root ("Ramp" / ramp.com -> "ramp"), so we
probe a few candidates on each ATS and keep the hits. Results are cached per company.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from applypilot import db
from applypilot.sources import SOURCES

YC_URL = "https://yc-oss.github.io/api/companies/all.json"
H1B_URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-2023.csv"

_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|lp|llp|plc|pbc|holdings"
    r"|group|usa|us|america|americas|technologies|technology|services|svcs|the)\b\.?",
    re.I,
)


def company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _SUFFIX.sub(" ", name.lower()))


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def domain_root(website: str) -> str:
    """The registrable name of a site: jobs.cable.tech -> "cable", acme.co.uk -> "acme"."""
    host = urlsplit(website if "//" in website else f"https://{website}").netloc.lower().split(":")[0]
    labels = [x for x in host.split(".") if x]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")


def slug_candidates(name: str, website: str | None = None) -> list[str]:
    out: list[str] = []
    words = re.findall(r"[a-z0-9]+", _SUFFIX.sub(" ", name.lower()))
    if words:
        # no first-word-only guess: "Scale AI" -> "scale" would hit unrelated companies' boards
        out += ["".join(words), "-".join(words)]
    if website:
        # the domain root goes last: figure.ai -> "figure" is a different company's board
        root = domain_root(website)
        if root:
            out.append(root)
    seen: set[str] = set()
    return [s for s in out if len(s) > 1 and not (s in seen or seen.add(s))]


_BOARD_WORDS = re.compile(r"\b(job board|career site|open positions|hiring|(jobs?|careers?)( at)?)\b", re.I)


def names_match(board_name: str, company: str) -> bool:
    """Greenhouse returns the board's display name; reject boards whose name is a longer, different
    company ("Oath Animal Hospital" for Oath Holdings). Allows short suffixes ("BILL" vs "Bill.com")
    and generic board words ("Rubrik Job Board")."""
    a, b = company_key(_BOARD_WORDS.sub(" ", board_name)), company_key(company)
    if not a or not b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short) and len(long_) - len(short) <= 4


@dataclass
class Seed:
    name: str
    website: str | None = None
    tier: str = "warm"
    source: str = "manual"
    h1b_approvals: int | None = None


async def probe(
    client: httpx.AsyncClient, seed: Seed, sem: asyncio.Semaphore
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for ats, src in SOURCES.items():
        for slug in slug_candidates(seed.name, seed.website):
            async with sem:
                try:
                    name = await src.exists(client, slug)
                except (httpx.HTTPError, ValueError):
                    name = None
            # Lever/Ashby echo the slug back (no display name), so only Greenhouse can be name-checked
            if name and (name == slug or names_match(name, seed.name)):
                found.append((str(ats), slug))
                break  # one board per ATS is enough
    return found


async def add_companies(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    seeds: Iterable[Seed],
    concurrency: int = 16,
    on_progress=None,
    negative_ttl_s: float = 30 * 86400,
) -> Counter:
    """Probe each company (skipping cached ones) and add every board found. Returns counts."""
    sem = asyncio.Semaphore(concurrency)
    counts: Counter = Counter()
    seeds = list(seeds)

    async def one(seed: Seed) -> None:
        key = company_key(seed.name)
        if not key:
            return
        cached = db.get_probe(conn, key, negative_ttl_s)
        found = cached if cached is not None else await probe(client, seed, sem)
        if cached is None:
            db.save_probe(conn, key, found, time.time())
            counts["probed"] += 1
        for ats, slug in found:
            new = db.add_board(conn, ats, slug, seed.name, seed.tier, seed.source, seed.h1b_approvals)
            counts["new_boards" if new else "existing_boards"] += 1
            counts[ats] += 1
        if not found:
            counts["no_board"] += 1
        if on_progress:
            on_progress()

    await asyncio.gather(*(one(s) for s in seeds))
    return counts


def yc_seeds(path_or_json: Path | list, us_only: bool = True) -> list[Seed]:
    data = path_or_json if isinstance(path_or_json, list) else json.loads(Path(path_or_json).read_text())
    out = []
    for c in data:
        if not c.get("isHiring") or c.get("status") != "Active":
            continue
        regions = set(c.get("regions") or [])
        if us_only and not regions & {"United States of America", "America / Canada", "Remote", "Fully Remote"}:
            continue
        out.append(Seed(c["name"], c.get("website"), "warm", "yc"))
    return out


def h1b_counts(csv_path: Path) -> Counter:
    """Employer name -> initial + continuing approvals in the USCIS Employer Data Hub export."""
    agg: Counter = Counter()
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            name = (r.get("Employer") or "").strip()
            if name:
                agg[name] += int(r.get("Initial Approval") or 0) + int(r.get("Continuing Approval") or 0)
    return agg


def h1b_seeds(csv_path: Path, min_approvals: int = 25, hot_threshold: int = 100) -> list[Seed]:
    return [
        Seed(name.title(), None, "hot" if n >= hot_threshold else "warm", "h1b", n)
        for name, n in h1b_counts(csv_path).most_common()
        if n >= min_approvals
    ]


def file_seeds(path: Path, tier: str = "hot") -> list[Seed]:
    """One company per line, optionally `Name, website`. '#' starts a comment."""
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, site = (p.strip() for p in line.partition(","))
        out.append(Seed(name, site or None, tier, "curated"))
    return out
