"use client";

import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { day, fmt } from "@/lib/api";
import type { SourceRef } from "@/lib/types";
import { Missing } from "./evidence";

/** Where a value came from: source, tier, retrieval date, the original, and the quote or location that anchors it. */
export function SourceLine({ source, quote, location, compact }: { source: SourceRef | null | undefined; quote?: string | null; location?: string | null; compact?: boolean }) {
  if (!source) return <Missing>Source not recorded</Missing>;
  return (
    <span className="text-data leading-snug text-ink-2">
      <Link href={`/sources?id=${source.id}`} className="font-medium">
        {compact ? source.tier_label : source.title}
      </Link>
      {!compact ? <span className="text-ink-3"> · {source.tier_label}</span> : null}
      <span className="text-ink-3"> · retrieved {day(source.retrieved_at)}</span>
      {source.url ? (
        // The new-tab notice is an aria-label, not a visually hidden span: an absolutely positioned span inside a
        // scrolling table escapes the table's clipping and widens the page.
        <a href={source.url} target="_blank" rel="noopener noreferrer" aria-label="Original source (opens in a new tab)" className="ml-1.5 inline-flex items-center gap-0.5 text-caption">
          original <ExternalLink aria-hidden size={11} />
        </a>
      ) : null}
      {quote ? <span className="mt-0.5 block text-ink-3">“{quote}”</span> : null}
      {location ? <span className="mono mt-0.5 block break-all text-ink-3">{location}</span> : null}
    </span>
  );
}

/**
 * A reported value with its interval. Formatting comes from `fmt` (owned with the charts); this component only
 * lays the pieces out. The percent sign is appended for percent units, as before.
 * `rejected` strikes the value through: a record rejected in review is kept for audit but must not read as usable.
 * Show the rejected VerificationBadge next to it, because a strike-through alone is not announced.
 */
export function ValueCell({ value, unit, lo, hi, level, rejected }: { value: number | null; unit?: string | null; lo?: number | null; hi?: number | null; level?: number | null; rejected?: boolean }) {
  const isPct = !!unit && /percent|%/i.test(unit);
  const figure = (
    <>
      {fmt(value)}
      {isPct ? "%" : ""}
    </>
  );
  return (
    <span className="num whitespace-nowrap">
      {rejected ? <s className="font-semibold text-ink-3 decoration-critical decoration-2">{figure}</s> : <span className="font-semibold text-ink">{figure}</span>}
      {lo != null && hi != null ? (
        <span className="ml-1.5 text-caption text-ink-3">
          {level ? `${fmt(level, 1)}% CI ` : "CI "}
          {fmt(lo)} to {fmt(hi)}
        </span>
      ) : null}
    </span>
  );
}
