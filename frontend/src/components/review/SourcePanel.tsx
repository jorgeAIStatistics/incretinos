"use client";

import { ExternalLink, FlaskConical } from "lucide-react";
import { Callout, Disclosure, Missing, TierBadge } from "@/components/ui";
import { titleCase } from "@/lib/api";
import { type EvidenceDetail, fieldLabel, highlightNumbers, shortHash, stamp, storedNumbers } from "@/lib/review";
import { Block, Fields, JsonBlock, MarkedText, Stored } from "./bits";
import { FullTextPanel } from "./FullTextPanel";

/*
 * LEFT panel: the original source and its context. What the reviewer checks the record against: the source, the
 * exact snapshot, the quoted passage with the stored numbers marked (or the registry JSON at the stored location),
 * and how the quote was anchored. Anchoring is described as what it is: the words exist in the source.
 */

const ANCHOR_SHORT: Record<string, string> = {
  FULL_TEXT: "Found in the stored full text",
  VERIFIED_EXCERPT: "Matches a verified excerpt",
  NOT_ANCHORED: "Not found in the source",
  NO_QUOTE: "No quote curated",
  STRUCTURED_PAYLOAD: "Read from the stored registry JSON",
  FULLTEXT_EXTRACTION: "Parsed from full-text cells: not verified",
};

export function SourcePanel({ detail }: { detail: EvidenceDetail }) {
  const source = detail.source ?? null;
  const anchoring = detail.anchoring ?? null;
  const numbers = storedNumbers(detail);
  const quote = anchoring?.quote ?? null;
  const found = quote ? highlightNumbers(quote, numbers).found : new Set<string>();
  const notFound = quote ? numbers.filter((n) => !found.has(n.field)) : [];
  const registry = detail.registry ?? null;
  const labels = Object.entries(registry?.labels ?? {});

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="source-panel">
      <Block title="Source" level={3}>
        {source ? (
          <>
            <p className="text-body font-medium leading-snug text-ink" data-testid="source-title">
              {source.url ? (
                <a href={source.url} target="_blank" rel="noreferrer noopener" className="text-accent hover:underline">
                  {source.title}
                  <ExternalLink aria-hidden size={12} className="ml-1 inline align-baseline" />
                  <span className="sr-only"> (opens the original in a new tab)</span>
                </a>
              ) : (
                source.title
              )}
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <TierBadge tier={source.tier} label={source.tier_label} full />
              {source.source_type ? <span className="text-caption text-ink-3">Source type: {source.source_type}</span> : null}
              {source.is_synthetic ? (
                <span className="hatch inline-flex items-center gap-1 rounded-chip border border-dashed border-ink-2 px-1.5 text-caption text-ink">
                  <FlaskConical aria-hidden size={12} /> Synthetic demonstration source
                </span>
              ) : null}
            </div>
          </>
        ) : (
          <Callout tone="warn" title="No source is linked to this record">
            Without a source the record cannot be checked. Request information or reject it.
          </Callout>
        )}
      </Block>

      {detail.fulltext ? <FullTextPanel ft={detail.fulltext} /> : null}

      {registry ? (
        <Block title="Registry record at the stored location" level={3} testId="registry-fragment">
          <p className="mono mb-2 break-all text-caption text-ink-2">{registry.location}</p>
          {registry.resolved === false ? (
            <Callout tone="warn" title="The location does not resolve in the stored registry snapshot">
              {registry.note ?? "The value cannot be traced to the stored JSON."}
            </Callout>
          ) : null}
          {labels.length ? (
            <Fields
              className="mb-2"
              rows={labels
                .filter(([, v]) => v === null || typeof v !== "object")
                .map(([k, v]) => [fieldLabel(k), v === null || v === undefined || v === "" ? <Missing key={k} className="!whitespace-normal">Not in the record</Missing> : <MarkedText key={k} text={String(v)} numbers={numbers} />, k])}
            />
          ) : null}
          {labels
            .filter(([, v]) => v !== null && typeof v === "object")
            .map(([k, v]) => (
              <Disclosure key={k} summary={`${titleCase(k)} (JSON)`} className="mb-1.5">
                <JsonBlock value={v} label={`Registry ${k.replace(/_/g, " ")}`} numbers={numbers} />
              </Disclosure>
            ))}
          {registry.fragment !== undefined && registry.fragment !== null ? (
            <>
              <p className="mb-1 mt-2 text-caption text-ink-3">The JSON the value was read from, with the stored numbers marked:</p>
              <JsonBlock value={registry.fragment} text={registry.fragment_text} label="Registry JSON fragment" numbers={numbers} />
              {registry.fragment_truncated ? <p className="mt-1 text-caption text-warn">The fragment is long and was shortened by the service.</p> : null}
            </>
          ) : null}
          {registry.note ? <p className="mt-2 text-caption text-ink-3">{registry.note}</p> : null}
        </Block>
      ) : null}

      <Block title="Quoted passage" level={3} testId="source-quote">
        {quote ? (
          <>
            <blockquote className="rounded-control border-l-[3px] border-l-ink-3 bg-sunken px-3 py-2 text-body leading-relaxed text-ink">
              <MarkedText text={quote} numbers={numbers} testId="quote-text" />
            </blockquote>
            <p className="mt-1.5 text-caption text-ink-3">
              <mark className="rounded-[2px] bg-accent-soft px-0.5 font-semibold text-ink underline decoration-accent decoration-2 underline-offset-[3px]">Shaded and underlined</mark> numbers are values this record stores.
              {found.size ? <span> Found in the quote: {numbers.filter((n) => found.has(n.field)).map((n) => `${fieldLabel(n.field)} ${String(n.value).replace(/^-/, "−")}`).join("; ")}.</span> : <span> None of the stored numbers appears in the quote.</span>}
              {notFound.length ? (
                <span className="text-ink-2">
                  {" "}
                  Stored but not in the quote: {notFound.map((n) => `${fieldLabel(n.field)} ${String(n.value).replace(/^-/, "−")}`).join("; ")}.
                </span>
              ) : null}
            </p>
            {anchoring?.locator ? <p className="mt-1 text-caption text-ink-2">Locator: {anchoring.locator}</p> : null}
          </>
        ) : anchoring?.basis === "STRUCTURED_PAYLOAD" ? (
          <p className="text-data text-ink-2">Registry rows have no quote: the value was read from the stored JSON shown above.</p>
        ) : (
          <Missing className="!whitespace-normal">No quote was curated for this record</Missing>
        )}
        {anchoring?.context ? (
          <Disclosure summary="Surrounding text from the stored source" className="mt-2">
            <p className="rounded-control border border-hairline px-3 py-2 text-data leading-relaxed text-ink-2">
              <MarkedText text={anchoring.context} numbers={numbers} />
            </p>
          </Disclosure>
        ) : null}
      </Block>

      <Block title="How the quote was anchored" level={3} testId="anchoring">
        {anchoring ? (
          <>
            <p className="text-data font-medium text-ink">{ANCHOR_SHORT[anchoring.basis] ?? titleCase(anchoring.basis)}</p>
            {anchoring.basis_label ? <p className="text-data text-ink-2">{anchoring.basis_label}.</p> : null}
            {anchoring.checked_against_snapshot_version != null ? <p className="mt-1 text-caption text-ink-3">Checked against snapshot version {anchoring.checked_against_snapshot_version}.</p> : null}
            <p className="mt-1.5 text-caption text-ink-2">{anchoring.note ?? "Anchoring shows that the quoted words are in the source. It does not show that the number belongs to this study, arm, endpoint, estimand or population."}</p>
          </>
        ) : (
          <Missing className="!whitespace-normal">The service did not report how this record was anchored</Missing>
        )}
      </Block>
      {source ? (
        <Block title="Source details" level={3} testId="source-details">
            <Fields
              
              rows={[
                ["Publisher", <Stored key="p" value={source.publisher} />],
                ["Published", <Stored key="d" value={source.published_date} />],
                ["Retrieved", stamp(source.snapshot_retrieved_at ?? source.retrieved_at)],
                [
                  "Snapshot version",
                  source.snapshot_version != null ? (
                    <span key="v">
                      {source.snapshot_version}
                      {source.latest_snapshot_version != null && source.latest_snapshot_version !== source.snapshot_version ? (
                        <span className="text-warn"> (a newer version, {source.latest_snapshot_version}, is stored)</span>
                      ) : source.latest_snapshot_version != null ? (
                        <span className="text-ink-3"> (latest stored)</span>
                      ) : null}
                      {source.version_label ? <span className="block text-caption text-ink-3">{source.version_label}</span> : null}
                    </span>
                  ) : (
                    <Missing key="v" className="!whitespace-normal">Not recorded</Missing>
                  ),
                ],
                [
                  "Content hash (SHA-256)",
                  source.content_hash ? (
                    <span key="h" className="mono break-all" title={source.content_hash}>
                      {shortHash(source.content_hash, 16)}
                    </span>
                  ) : (
                    <Missing key="h" className="!whitespace-normal">Not recorded</Missing>
                  ),
                ],
                ["Full text stored", source.stores_full_text ? "Yes" : "No: only the address, retrieval date and short quotes are stored"],
                ["Source key", <span key="k" className="mono break-all">{source.key}</span>],
              ]}
            />
            {source.license_note ? <p className="mt-2 text-caption text-ink-3">Licence: {source.license_note}</p> : null}
        </Block>
      ) : null}
    </div>
  );
}
