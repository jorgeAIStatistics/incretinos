"""The one rule that decides whether a curated quote is mechanically anchored.

The curated loader applies it when a row is created; the review lifecycle applies it again after
every load, against the newest stored snapshot of the cited source, so a row cannot stay
QUOTE_ANCHORED after its quote has left the source text.

What "anchored" means, and what it does not: the quote was found in the stored full text of the
source (``FULL_TEXT``), or it equals or lies inside one of the short excerpts that were verified
against the live source when fixtures were recorded (``VERIFIED_EXCERPT``; used where the full text
may not be redistributed). Either way it shows that the words exist in the source. It does not show
that the number was attributed to the right study, arm, endpoint, estimand or population.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from ..enums import VerificationStatus
from ..ingestion.snapshots import normalize_ws, quote_in_text

FULL_TEXT = "FULL_TEXT"
VERIFIED_EXCERPT = "VERIFIED_EXCERPT"
NOT_ANCHORED = "NOT_ANCHORED"
NO_QUOTE = "NO_QUOTE"
STRUCTURED_PAYLOAD = "STRUCTURED_PAYLOAD"  # registry rows: parsed from stored JSON, no quote involved
FULLTEXT_EXTRACTION = "FULLTEXT_EXTRACTION"  # full-text rows: parsed from document cells; never mechanically verified

ANCHOR_BASIS_LABEL = {
    FULL_TEXT: "Quote found in the stored full text of the source",
    VERIFIED_EXCERPT: "Quote matches an excerpt that was verified against the live source when it was recorded (the full text is not stored)",
    NOT_ANCHORED: "Quote not found in the stored text or verified excerpts of the source",
    NO_QUOTE: "No locator quote was curated",
    STRUCTURED_PAYLOAD: "Parsed from the stored structured registry payload",
    FULLTEXT_EXTRACTION: "Parsed from the cells (or a sentence) of a full-text document; a parse is not a verification, so it waits for a person",
}


@dataclass(frozen=True)
class AnchorResult:
    status: str  # QUOTE_ANCHORED | PENDING_HUMAN_REVIEW
    basis: str  # FULL_TEXT | VERIFIED_EXCERPT | NOT_ANCHORED | NO_QUOTE

    @property
    def anchored(self) -> bool:
        return self.status == VerificationStatus.QUOTE_ANCHORED.value


_ANCHORED_FULL = AnchorResult(VerificationStatus.QUOTE_ANCHORED.value, FULL_TEXT)
_ANCHORED_EXCERPT = AnchorResult(VerificationStatus.QUOTE_ANCHORED.value, VERIFIED_EXCERPT)
_PENDING = AnchorResult(VerificationStatus.PENDING_HUMAN_REVIEW.value, NOT_ANCHORED)
_NO_QUOTE = AnchorResult(VerificationStatus.PENDING_HUMAN_REVIEW.value, NO_QUOTE)


def anchor(quote: str | None, text: str | None, excerpts: Sequence[str] | None) -> AnchorResult:
    """Decide the mechanical status of a curated statement from its locator quote."""
    if not quote:
        return _NO_QUOTE
    if text and quote_in_text(quote, text):
        return _ANCHORED_FULL
    normalized = normalize_ws(quote).lower()
    for excerpt in excerpts or ():
        candidate = normalize_ws(excerpt).lower()
        if normalized == candidate or normalized in candidate:
            return _ANCHORED_EXCERPT
    return _PENDING


class AnchorCache:
    """Memoises anchoring per (source content hash, excerpt list, quote).

    Stored snapshot content is immutable for a given content hash, so the answer for a quote can be
    reused across reconcile passes in one process; only the excerpt list can grow, and it is part of
    the cache key.
    """

    def __init__(self, limit: int = 20000) -> None:
        self._results: OrderedDict[tuple[str, int, str], AnchorResult] = OrderedDict()
        self._limit = limit

    def get(self, content_hash: str, excerpts: Sequence[str], quote: str | None, load_text) -> AnchorResult:
        if not quote:
            return _NO_QUOTE
        key = (content_hash, hash(tuple(excerpts)), quote)
        found = self._results.get(key)
        if found is None:
            found = anchor(quote, load_text(), excerpts)
            self._results[key] = found
            if len(self._results) > self._limit:
                self._results.popitem(last=False)
        return found


ANCHOR_CACHE = AnchorCache()
