"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  Async,
  Callout,
  type Column,
  DataTable,
  Disclosure,
  Drawer,
  EmptyState,
  EpistemicBadge,
  ErrorState,
  Hypothetical,
  KV,
  Loading,
  Missing,
  Pagination,
  Panel,
  SearchField,
  Select,
  TableCard,
  TableSkeleton,
  TierBadge,
  VerificationBadge,
} from "@/components/ui";
import { day, fmt, useApi, useDebounced, withQuery } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { useMediaQuery, useQueryParams } from "@/lib/hooks";
import { usePagedList } from "@/lib/paging";
import type { SourceDetail as SourceDetailData, SourceListExtra, SourceListItem } from "@/lib/types";

/** Display names for source types. An unlisted type is shown by its code, never hidden. */
const TYPE: Record<string, string> = {
  CTGOV: "ClinicalTrials.gov",
  PUBMED: "PubMed",
  OPENFDA_LABEL: "FDA labeling",
  OPENFDA_FAERS: "FAERS (openFDA)",
  OPENFDA_LABEL_SEARCH: "FDA label database check",
  WEB: "Web (release, review, pipeline)",
  GUIDANCE: "Guidance",
};
const typeLabel = (code: string) => TYPE[code] ?? code;

function SourceDetail({ id, headingLevel }: { id: number; headingLevel: 2 | 3 }) {
  const state = useApi<SourceDetailData>(`/sources/${id}`);
  return (
    <Async state={state} errorTitle="This source could not be loaded" loading={<Loading rows={6} label="Loading source…" />}>
      {(s) => {
        const latest = s.snapshots.at(-1);
        const quotes = latest?.anchored_excerpts ?? [];
        const shown = s.dependent_findings.length;
        const total = s.dependent_findings_total ?? shown;
        return (
          <div className="fade-in space-y-5">
            <div>
              <p className="flex flex-wrap items-center gap-2 text-data text-ink-3">
                {typeLabel(s.source_type)} <TierBadge tier={s.tier} label={s.tier_label} />
              </p>
              {headingLevel === 2 ? <h2 className="mt-1.5 text-title font-semibold leading-snug text-ink">{s.title}</h2> : <h3 className="mt-1.5 text-title font-semibold leading-snug text-ink">{s.title}</h3>}
            </div>
            <KV
              rows={[
                ["Source key", <span key="k" className="mono break-all" translate="no">{s.key}</span>],
                ["Publisher", s.publisher],
                ["Published / updated", s.published_date],
                ["Retrieved", day(s.retrieved_at)],
                ["Version", s.version_label],
                ["Content hash (SHA-256)", s.content_hash ? <span key="h" className="mono break-all" translate="no">{s.content_hash}</span> : null],
                [
                  "Original",
                  s.url ? (
                    <a key="u" href={s.url} target="_blank" rel="noopener noreferrer" aria-label={`${s.url} (opens in a new tab)`} className="break-all">
                      {s.url}
                    </a>
                  ) : null,
                ],
                ["Storage and licence", s.license_note],
              ]}
            />

            <section aria-label="Snapshots">
              <p className="mb-1.5 text-data font-semibold text-ink">
                Snapshots <span className="num font-normal text-ink-3">({formatCount(s.snapshots.length)})</span>
              </p>
              <TableCard label="Snapshots of this source" maxHeight="320px">
                <table className="tbl tbl-compact" style={{ minWidth: 480 }}>
                  <thead>
                    <tr>
                      <th scope="col">Version</th>
                      <th scope="col">Retrieved</th>
                      <th scope="col">Hash</th>
                      <th scope="col">Text stored</th>
                      <th scope="col" className="r">Quotes anchored</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.snapshots.map((v) => (
                      <tr key={v.version_no}>
                        <td className="whitespace-nowrap">
                          {v.version_no}
                          {v.is_synthetic ? <span className="ml-1.5"><EpistemicBadge kind="SIMULATION" /></span> : null}
                        </td>
                        <td className="whitespace-nowrap">{day(v.retrieved_at)}</td>
                        <td className="mono whitespace-nowrap" translate="no">{v.content_hash.slice(0, 12)}…</td>
                        <td>{v.content_stored ? "Yes (public-domain / CC0)" : "No (hash and excerpts only)"}</td>
                        <td className="r">{formatCount(v.anchored_excerpts?.length ?? 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableCard>
              {quotes.length ? (
                <Disclosure summary={`Anchored locator quotes in the latest snapshot (${formatCount(quotes.length)})`} className="mt-2">
                  <ul className="list-disc space-y-1 pl-5 text-data text-ink-2">
                    {quotes.map((q, i) => (
                      <li key={`${i}:${q}`}>“{q}”</li>
                    ))}
                  </ul>
                </Disclosure>
              ) : null}
            </section>

            {shown ? (
              <section aria-label="Findings that depend on this source">
                <p className="mb-1.5 text-data font-semibold text-ink">
                  Findings that depend on this source{" "}
                  <span className="num font-normal text-ink-3">
                    ({formatCount(shown)}
                    {total > shown ? ` of ${formatCount(total)}` : ""})
                  </span>
                </p>
                <TableCard label="Findings that depend on this source" maxHeight="320px">
                  <table className="tbl tbl-compact" style={{ minWidth: 620 }}>
                    <thead>
                      <tr>
                        <th scope="col">Study</th>
                        <th scope="col">Arm</th>
                        <th scope="col">Endpoint</th>
                        <th scope="col" className="r">Value</th>
                        <th scope="col">Verification</th>
                      </tr>
                    </thead>
                    <tbody>
                      {s.dependent_findings.map((f) => (
                        <tr key={f.result_id}>
                          <td className="whitespace-nowrap">{f.study}</td>
                          <td className="min-w-[120px]">{f.arm}</td>
                          <td className="min-w-[220px]">{f.endpoint}</td>
                          <td className="r">{f.value === null || f.value === undefined ? <Missing /> : <Link href={`/compare?a=${f.result_id}`}>{fmt(f.value)}</Link>}</td>
                          <td><VerificationBadge status={f.verification_status} compact /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableCard>
                {/* Older API versions cap this list without reporting how many findings exist; say so rather than imply it is complete. */}
                {s.dependent_findings_total === undefined && shown >= 60 ? <p className="mt-1.5 text-caption font-medium text-warn">The service returns at most {formatCount(shown)} findings here and does not report the full number. More may depend on this source.</p> : null}
              </section>
            ) : (
              <p className="text-data text-ink-3">No stored result cites this source directly.</p>
            )}

            {s.changes.length ? (
              <section aria-label="Detected changes">
                <p className="mb-1.5 text-data font-semibold text-ink">Detected changes</p>
                <ul className="space-y-2">
                  {s.changes.map((c) => (
                    <li key={c.id}>
                      {c.is_synthetic ? (
                        <Hypothetical label={`Synthetic demonstration change, detected ${day(c.detected_at)}`}>
                          {c.summary} <Link href="/sources?tab=changes">Open in the review queue</Link>
                        </Hypothetical>
                      ) : (
                        <Callout tone="info" title={`Detected ${day(c.detected_at)}`}>
                          {c.summary} <Link href="/sources?tab=changes">Open in the review queue</Link>
                        </Callout>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </div>
        );
      }}
    </Async>
  );
}

export function SourcesTab() {
  const [param, setParams] = useQueryParams();
  const selected = Number(param("id")) || null;
  const type = param("type");
  const [q, setQ] = useState(param("q"));
  const query = useDebounced(q.trim(), 250);
  useEffect(() => setParams({ q: query }), [query, setParams]);
  const wide = useMediaQuery("(min-width: 1280px)");

  const list = usePagedList<SourceListItem, SourceListExtra>(withQuery("/sources", { source_type: type, q: query.length > 1 ? query : "" }), { pageSize: 25, legacyLimit: 2000 });
  const byType = list.data?.extra.by_type ?? {};
  const typeCodes = Array.from(new Set([...Object.keys(byType), ...(type ? [type] : [])])).sort((a, b) => typeLabel(a).localeCompare(typeLabel(b)));
  const hrefFor = (id: number) => withQuery("/sources", { id, type, q: query });

  const columns: Column<SourceListItem>[] = [
    {
      key: "source",
      header: "Source",
      rowHeader: true,
      className: "min-w-[220px] max-w-md",
      cell: (s) => (
        <>
          <Link href={hrefFor(s.id)} scroll={false} replace aria-current={selected === s.id ? "true" : undefined} className="font-medium">
            {s.title}
          </Link>
          <span className="mono block break-all text-ink-3" translate="no">{s.key}</span>
        </>
      ),
    },
    { key: "tier", header: "Tier", cell: (s) => <TierBadge tier={s.tier} label={s.tier_label} /> },
    { key: "retrieved", header: "Retrieved", className: "whitespace-nowrap", cell: (s) => day(s.retrieved_at) },
    { key: "findings", header: "Findings", align: "right", cell: (s) => formatCount(s.n_results) },
  ];

  const detail = selected ? <SourceDetail key={selected} id={selected} headingLevel={wide ? 2 : 3} /> : null;
  const intro = (
    <Callout tone="info" title="Select a source" level={2}>
      Every source records its tier, retrieval date, content hash and versions. Registry records, FDA labels and FAERS counts are stored in full; abstracts, press releases and review documents are represented by a hash and the short quotes that anchor curated values.
    </Callout>
  );

  return (
    <div className="grid grid-cols-1 items-start gap-5 xl:grid-cols-2">
      <div className="min-w-0">
        <form role="search" aria-label="Filter sources" onSubmit={(e) => e.preventDefault()} className="mb-3 flex flex-wrap items-end gap-3">
          <Select
            label="Source type"
            name="type"
            value={type}
            onChange={(v) => setParams({ type: v })}
            options={[{ value: "", label: "All types" }, ...typeCodes.map((code) => ({ value: code, label: byType[code] !== undefined ? `${typeLabel(code)} (${formatCount(byType[code])})` : typeLabel(code) }))]}
            className="w-full sm:w-64"
          />
          <SearchField label="Search" value={q} onChange={setQ} placeholder="Title, NCT, PMID…" hint={q.trim().length === 1 ? "Type at least 2 characters." : undefined} className="w-full sm:w-60" />
        </form>
        {list.status === "error" ? (
          <ErrorState title="The source list could not be loaded" message={list.error ?? ""} onRetry={list.reload} />
        ) : !list.data ? (
          <TableSkeleton rows={8} columns={4} label="Loading sources…" />
        ) : (
          <DataTable
            label="Evidence sources"
            columns={columns}
            rows={list.data.rows}
            rowKey={(s) => s.id}
            selectedKey={selected}
            refreshing={list.refreshing}
            minWidth={540}
            empty={<EmptyState title="No source matches" className="m-3 border-0">Try another source type or a different search term.</EmptyState>}
            footer={<Pagination page={list.page} pageSize={list.pageSize} total={list.data.total} reachable={list.data.reachable} truncated={list.data.truncated} onPage={list.setPage} onPageSize={list.setPageSize} noun="sources" />}
          />
        )}
      </div>

      {/* Wide screens show the detail beside the list; narrower ones open it in a drawer. Nothing is drawn until the width is known. */}
      {wide === null ? null : wide ? (
        <div className="min-w-0 xl:sticky xl:top-6">{detail ? <Panel as="section" className="!p-5">{detail}</Panel> : intro}</div>
      ) : (
        <>
          {selected ? null : intro}
          <Drawer open={selected !== null} onOpenChange={(open) => !open && setParams({ id: null })} title="Source detail" width="34rem">
            <div className="px-4 py-4">{detail}</div>
          </Drawer>
        </>
      )}
    </div>
  );
}
