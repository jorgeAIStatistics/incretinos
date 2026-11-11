import clsx from "clsx";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

/*
 * Two quiet ways to lay out counts and dated items without a card around each one (the anti-nested-box rule):
 *
 *   Ledger / LedgerItem   a row of headline counts in plain layout, separated by hairlines. A count that has a page
 *                         listing exactly what it counts is a link to that page; a count without one is plain text.
 *   Timeline / TimelineItem  a dated list on a vertical rule: the date sits on the rail, the item beside it.
 *
 * Neither animates its values. Both collapse to a single column on a phone.
 */

export function Ledger({ label, children, className }: { /** Names the list for assistive technology. */ label: string; children: ReactNode; className?: string }) {
  return (
    <dl aria-label={label} className={clsx("grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:flex lg:flex-wrap lg:gap-x-0", className)}>
      {children}
    </dl>
  );
}

/**
 * One count in a Ledger. `value` is already formatted (counts through `formatCount`); `href` makes the value a link
 * to the page that lists the same records, and `linkLabel` names that link for assistive technology. A value of
 * zero is still shown: a count is never dropped because it is small.
 */
export function LedgerItem({ label, value, note, href, linkLabel, testId }: { label: ReactNode; value: ReactNode; note?: ReactNode; href?: string | null; linkLabel?: string; testId?: string }) {
  return (
    <div className="min-w-0 lg:border-l lg:border-hairline lg:px-5 lg:first:border-l-0 lg:first:pl-0" data-testid={testId}>
      <dt className="text-caption font-medium text-ink-3">{label}</dt>
      <dd className="mt-0.5 text-stat font-semibold leading-tight text-ink">
        {href ? (
          <Link href={href} aria-label={linkLabel} className="text-ink decoration-accent hover:text-accent">
            {value}
          </Link>
        ) : (
          value
        )}
      </dd>
      {note ? <dd className="mt-0.5 text-caption text-ink-3">{note}</dd> : null}
    </div>
  );
}

/** A link that says where it goes next: text, then an arrow. Used under a section to open its full view. */
export function NextLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return (
    <Link href={href} className={clsx("inline-flex items-center gap-1 text-data font-medium", className)}>
      {children}
      <ArrowRight aria-hidden size={13} className="shrink-0" />
    </Link>
  );
}

export function Timeline({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <ol aria-label={label} className={clsx("relative ml-1 border-l border-rule", className)}>
      {children}
    </ol>
  );
}

/**
 * One dated entry. `date` is printed as the source gives it (registry dates are often partial: "2026-11"); `dateNote`
 * qualifies it ("estimated", "actual"). The marker on the rule is a plain tick, not a coloured status dot.
 */
export function TimelineItem({ date, dateNote, children, testId }: { date: ReactNode; dateNote?: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <li className="relative pb-4 pl-4 last:pb-0" data-testid={testId}>
      <span aria-hidden className="absolute -left-1 top-[9px] h-px w-3 bg-control" />
      <p className="num text-caption font-medium text-ink-2">
        {date}
        {dateNote ? <span className="font-normal text-ink-3"> ({dateNote})</span> : null}
      </p>
      <div className="mt-0.5 min-w-0 text-data text-ink">{children}</div>
    </li>
  );
}
