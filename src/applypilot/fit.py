"""Resume fit: score a job description against each pre-built resume variant with the resumetailor
skill's own ATS scorer (`ats_score.py`, imported, not shelled out), and pick the best variant.

Variant PDFs are read once and their text cached; they're re-read when a PDF changes. With no
variants built yet (or no scorer), fit is simply unavailable and ranking works without it.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

from applypilot.config import Settings
from applypilot.tailor import VARIANTS, variant_pdf

log = logging.getLogger("applypilot.fit")


class Fitter:
    def __init__(self, scorer: Path, variants: dict[str, Path]) -> None:
        self.mod = _load(scorer)
        self.variants = variants
        self._text: dict[str, tuple[float, tuple[str, int | None]]] = {}
        original = self.mod.pdf_text

        def cached(path):  # pdfminer takes ~0.2 s per page; a backfill scores thousands of jobs
            p = str(path)
            mtime = Path(p).stat().st_mtime
            hit = self._text.get(p)
            if hit is None or hit[0] != mtime:
                hit = self._text[p] = (mtime, original(p))
            return hit[1]

        self.mod.pdf_text = cached
        # score() re-derives the JD's terms for every variant (~40 ms each); derive them once per JD.
        # score() mutates the returned dict, so each call gets a copy
        terms = getattr(self.mod, "jd_terms", None)
        last: dict[str, tuple] = {}

        def cached_terms(jd):
            if jd not in last:
                last.clear()
                last[jd] = terms(jd)
            weights, groups = last[jd]
            return dict(weights), [(list(g), w) for g, w in groups]

        if callable(terms):
            self.mod.jd_terms = cached_terms

    def scores(self, jd: str) -> dict[str, int]:
        return {name: self.mod.score(str(pdf), jd)["total"] for name, pdf in self.variants.items() if pdf.exists()}

    def best(self, jd: str) -> tuple[str, int] | None:
        s = self.scores(jd)
        return max(s.items(), key=lambda kv: kv[1]) if s else None


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("ats_score", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cache: dict[str, Fitter | None] = {}


def get(settings: Settings) -> Fitter | None:
    """The shared fitter, or None when the scorer or every variant PDF is missing."""
    key = settings.tailor.scorer
    if key not in _cache:
        scorer = Path(settings.tailor.scorer).expanduser()
        variants = {v: variant_pdf(settings, v) for v in VARIANTS}
        if not scorer.exists() or not any(p.exists() for p in variants.values()):
            return None  # not cached: variants may be built later in this process
        try:
            _cache[key] = Fitter(scorer, variants)
        except Exception as e:  # a broken scorer must never stop polling
            log.warning("fit scorer unavailable: %r", e)
            _cache[key] = None
    return _cache[key]


def jd_text(title: str, company: str, description: str) -> str:
    return f"{title}\n{company}\n\n{description}"
