"""Verify curated locator quotes against live web sources without redistributing their text.

For each curated web source the page is fetched once, reduced to plain text in memory,
hashed, and every curated quote citing it is checked for a verbatim (whitespace- and
typography-normalized) match. Only the hash, the retrieval time and the quotes that
matched are written to ``web_anchors.json``; the page text itself is discarded.
Sources that refuse automated retrieval are recorded as such and their statements stay
CURATED_PENDING_REVIEW.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..ingestion.snapshots import quote_in_text, sha256_text
from .loader import load_yaml, read_drugs, read_sources, study_files
from .schema import ClaimsFile

_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def html_to_text(markup: str) -> str:
    text = _TAG.sub(" ", markup)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def collect_web_quotes(curated_dir: Path) -> dict[str, list[str]]:
    quotes: dict[str, list[str]] = {}

    def add(source: str | None, *items: str | None) -> None:
        if source and source.startswith(("web:", "guidance:")):
            bucket = quotes.setdefault(source, [])
            bucket.extend(q for q in items if q and q not in bucket)

    for drug in read_drugs(curated_dir).drugs:
        add(drug.regulatory.source, drug.regulatory.quote)
        for item in [*drug.mechanisms, *drug.indications, *drug.equivalences]:
            add(item.source, item.quote)
    for file in study_files(curated_dir):
        for item in [*file.estimands, *file.claims]:
            add(item.source, item.quote)
        for item in [*file.results, *file.analyses, *file.adverse_events]:
            add(item.source, item.quote, *item.extra_quotes)
    claims_path = curated_dir / "claims.yaml"
    if claims_path.is_file():
        for claim in load_yaml(claims_path, ClaimsFile).claims:
            add(claim.source, claim.quote)
    return quotes


def anchor_all(curated_dir: Path, *, client: httpx.Client | None = None, pause_s: float = 1.0) -> dict:
    quotes = collect_web_quotes(curated_dir)
    sources = {s.key: s for s in read_sources(curated_dir)}
    out_path = curated_dir / "web_anchors.json"
    anchors: dict = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {}
    client = client or httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "IncretinOS/0.1 (citation verification; fetches each cited page once)", "Accept": "text/html"},
    )
    summary = {"sources": 0, "fetched": 0, "quotes": 0, "anchored": 0, "unreachable": []}
    for key, wanted in sorted(quotes.items()):
        src = sources.get(key)
        if src is None or not src.url or src.url.lower().endswith(".pdf"):
            continue
        summary["sources"] += 1
        summary["quotes"] += len(wanted)
        try:
            response = client.get(src.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            anchors[key] = {**anchors.get(key, {}), "fetch_status": f"unreachable: {type(exc).__name__}", "checked_at": datetime.now(UTC).isoformat()}
            summary["unreachable"].append(key)
            continue
        text = html_to_text(response.text)
        verified = [q for q in wanted if quote_in_text(q, text)]
        anchors[key] = {
            "url": src.url,
            "fetch_status": "ok",
            "checked_at": datetime.now(UTC).isoformat(),
            "content_sha256": sha256_text(text),
            "text_chars": len(text),
            "verified_quotes": verified,
            "unverified_quote_count": len(wanted) - len(verified),
        }
        summary["fetched"] += 1
        summary["anchored"] += len(verified)
        time.sleep(pause_s)
    out_path.write_text(json.dumps(anchors, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return summary
