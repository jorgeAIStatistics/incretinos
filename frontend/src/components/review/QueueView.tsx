"use client";

import { ChevronRight, FilterX } from "lucide-react";
import Link from "next/link";
import { type RefObject, useEffect, useRef, useState } from "react";
import { Button, type Column, DataTable, Disclosure, EmptyState, ErrorState, Missing, Pagination, SearchField, Select, TableSkeleton, TextField, TierBadge, VerificationBadge } from "@/components/ui";
import { titleCase, useApi, useDebounced, withQuery } from "@/lib/api";
import { canonicalStatus, isTier, TIERS, VERIFICATION, VERIFICATION_ORDER } from "@/lib/evidence";
import { formatCount } from "@/lib/format";
import { CHANGED_OPTIONS, decisionLabel, type EvidenceListItem, type EvidenceListResponse, evidenceTypeLabel, fieldLabel, type QueueFilters, queuePath, SORTS, stamp } from "@/lib/review";
import { Severity } from "./bits";
import { Kbd, useShortcuts } from "./ShortcutHelp";

/*
 * The review queue: every reviewable record that matches the filters, with true totals from the service and
 * facet counts per filter. The highlighted row (the cursor) is part of the address (`pos`, its index in the whole
 * filtered queue), so j/k, paging, opening a record and coming back all keep the reviewer's place.
 */

export const PAGE_SIZES = [25, 50, 100];

type Facets = Record<string, Record<string, number>>;

function facetOptions(facets: Facets | undefined, facet: string, current: string | undefined, label: (v: string) => string, anyLabel = "Any") {
  const counts = facets?.[facet] ?? {};
  const values = Object.keys(counts);
  if (current && !values.includes(current)) values.unshift(current);
  return [{ value: "", label: anyLabel }, ...values.map((v) => ({ value: v, label: `${label(v)} (${formatCount(counts[v] ?? 0)})` }))];
}

const statusName = (s: string) => {
  const c = canonicalStatus(s);
  return c ? VERIFICATION[c].label : titleCase(s);
};

export function QueueView({
  filters,
  pos,
  size,
  setParams,
  hrefFor,
  onOpen,
  searchRef,
}: {
  filters: QueueFilters;
  pos: number;
  size: number;
  setParams: (patch: Record<string, string | null>) => void;
  hrefFor: (row: EvidenceListItem, pos: number) => string;
  onOpen: (row: EvidenceListItem, pos: number) => void;
  searchRef: RefObject<HTMLDivElement | null>;
}) {
  const page = Math.floor(pos / size);
  const base = queuePath(filters);
  const list = useApi<EvidenceListResponse>(withQuery(base, { limit: size, offset: page * size }), { keepPrevious: true });
  const data = list.data;
  const rows = data?.rows ?? [];
  const total = data?.total ?? 0;
  const cursor = data && !list.stale ? pos - page * size : -1;
  const facets = data?.facets;

  const [q, setQ] = useState(filters.q ?? "");
  const debounced = useDebounced(q, 300);
  useEffect(() => {
    if ((filters.q ?? "") !== debounced) setParams({ q: debounced || null, pos: null });
    // Only the debounced text drives the address: reacting to `filters.q` here would write a stale value back after "Clear filters".
    // oxlint-disable-next-line react/exhaustive-deps
  }, [debounced]);

  const set = (key: string) => (value: string) => setParams({ [key]: value || null, pos: null });
  const secondary = ["tier", "source_type", "category", "risk", "missing", "origin", "source_key"] as const;
  const anyFilter = Object.entries(filters).some(([k, v]) => k !== "sort" && k !== "desc" && !!v);

  // The address updates in a transition, so a fast run of key presses could read an outdated position. The latest
  // position is therefore also kept here and updated at once by every key press.
  const live = useRef(pos);
  useEffect(() => {
    live.current = pos;
  }, [pos]);
  const moveTo = (next: number) => {
    live.current = next;
    setParams({ pos: next > 0 ? String(next) : null });
  };
  const openLive = () => {
    const i = live.current - page * size;
    if (data && !list.stale && rows[i]) onOpen(rows[i], live.current);
  };
  useShortcuts({
    j: () => (data && live.current + 1 < total ? moveTo(live.current + 1) : undefined),
    k: () => (live.current > 0 ? moveTo(live.current - 1) : undefined),
    Enter: openLive,
    o: openLive,
    "/": () => searchRef.current?.querySelector<HTMLInputElement>('input[type="search"]')?.focus(),
  });

  // Keep the highlighted row in view as j/k move it.
  const tableRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    tableRef.current?.querySelector('tr[data-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [cursor, data]);

  const columns: Column<EvidenceListItem>[] = [
    {
      key: "status",
      header: "Status",
      className: "whitespace-nowrap",
      cell: (r) => (
        <div className="flex flex-col items-start gap-1">
          <VerificationBadge status={r.verification_status} compact />
          {r.changed_since_review ? <span className="text-caption font-medium text-warn">Changed since review</span> : null}
          {r.changed_since_last_seen ? <span className="text-caption font-medium text-ink-2">Newly updated</span> : null}
          {r.review_hold ? (
            <span className="text-caption font-medium text-warn" title={r.exclusion_reason ?? undefined} data-testid="queue-review-hold">
              On review hold
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: "record",
      header: "Record",
      rowHeader: true,
      className: "min-w-[280px] max-w-[30rem]",
      cell: (r, i) => (
        <div className="flex gap-1">
          {i === cursor ? <ChevronRight aria-hidden size={14} className="mt-0.5 shrink-0 text-accent" /> : <span className="w-[14px] shrink-0" />}
          <div className="min-w-0">
            <Link href={hrefFor(r, page * size + i)} className="font-medium text-accent hover:underline" aria-current={i === cursor ? "true" : undefined} data-testid="queue-record-link">
              {r.label}
            </Link>
            {i === cursor ? <span className="sr-only"> (highlighted)</span> : null}
            {r.value_summary ? <p className="mt-0.5 line-clamp-2 text-caption text-ink-2">{r.value_summary}</p> : null}
          </div>
        </div>
      ),
    },
    {
      key: "trial",
      header: "Trial",
      className: "whitespace-nowrap",
      cell: (r) =>
        r.study_nct ? (
          <>
            {r.study_name ?? r.study_nct}
            <span className="mono block text-caption text-ink-3">{r.study_nct}</span>
          </>
        ) : (
          <Missing>No trial</Missing>
        ),
    },
    {
      key: "type",
      header: "Type",
      className: "whitespace-nowrap",
      cell: (r) => (
        <>
          {evidenceTypeLabel(r.evidence_type)}
          <span className="block text-caption text-ink-3">{r.origin ? titleCase(r.origin) : ""}</span>
        </>
      ),
    },
    { key: "source", header: "Source", className: "whitespace-nowrap", cell: (r) => <TierBadge tier={r.tier} label={r.tier_label} /> },
    {
      key: "risk",
      header: "Checks",
      className: "whitespace-nowrap",
      cell: (r) => (
        <>
          <Severity level={r.risk_level} />
          <span className="block text-caption text-ink-3">{r.n_flags ? `${r.n_flags} ${r.n_flags === 1 ? "flag" : "flags"}` : "No flags"}</span>
        </>
      ),
    },
    {
      key: "missing",
      header: "Missing",
      className: "min-w-[8rem] max-w-[12rem] text-caption text-ink-2",
      cell: (r) => (r.missing_fields?.length ? r.missing_fields.map(fieldLabel).join(", ") : <span className="text-ink-3">Nothing listed</span>),
    },
    {
      key: "decision",
      header: "Last decision",
      className: "whitespace-nowrap text-caption",
      cell: (r) =>
        r.last_decision ? (
          <>
            {decisionLabel(r.last_decision.decision)}
            <span className="block text-ink-3">
              {r.last_decision.reviewer ?? ""} {stamp(r.last_decision.decided_at)}
            </span>
          </>
        ) : (
          <span className="text-ink-3">{r.open_requests ? `${r.open_requests} open request${r.open_requests === 1 ? "" : "s"}` : "None"}</span>
        ),
    },
  ];

  return (
    <section aria-labelledby="queue-heading" data-testid="review-queue">
      <h2 id="queue-heading" className="sr-only">
        Review queue
      </h2>
      <div ref={searchRef} className="mb-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <SearchField label="Search" value={q} onChange={setQ} onBlur={() => setParams({ q: q || null, pos: null })} placeholder="Trial, arm, source, key…" className="sm:col-span-2 lg:col-span-1 xl:col-span-2" />
        <Select label="Evidence type" value={filters.etype ?? ""} onChange={set("etype")} options={facetOptions(facets, "evidence_type", filters.etype, evidenceTypeLabel)} />
        <Select
          label="Verification status"
          value={filters.status ?? ""}
          onChange={set("status")}
          options={facetOptions(facets, "status", filters.status, statusName).sort((a, b) => (a.value === "" ? -1 : b.value === "" ? 1 : VERIFICATION_ORDER.indexOf(canonicalStatus(a.value) ?? "REJECTED") - VERIFICATION_ORDER.indexOf(canonicalStatus(b.value) ?? "REJECTED")))}
        />
        <Select label="Drug" value={filters.drug ?? ""} onChange={set("drug")} options={facetOptions(facets, "drug", filters.drug, (v) => v)} />
        <Select label="Changed" value={filters.changed ?? ""} onChange={set("changed")} options={CHANGED_OPTIONS.map((o) => ({ ...o }))} />
      </div>
      <Disclosure summary="More filters: trial, source, endpoint, risk, missing field" defaultOpen={secondary.some((k) => !!filters[k]) || !!filters.nct} className="mb-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <TextField
            label="Trial (NCT number)"
            value={filters.nct ?? ""}
            onChange={(v) => setParams({ nct: v.trim() || null, pos: null })}
            placeholder="NCT05869903…"
            error={filters.nct && !/^NCT\d{8}$/i.test(filters.nct) ? "An NCT number is NCT followed by 8 digits." : null}
          />
          <Select label="Source tier" value={filters.tier ?? ""} onChange={set("tier")} options={facetOptions(facets, "tier", filters.tier, (v) => (isTier(v) ? TIERS[v].label : titleCase(v)))} />
          <Select label="Source type" value={filters.source_type ?? ""} onChange={set("source_type")} options={facetOptions(facets, "source_type", filters.source_type, (v) => v)} />
          <Select label="Endpoint category" value={filters.category ?? ""} onChange={set("category")} options={facetOptions(facets, "endpoint_category", filters.category, titleCase)} />
          <Select label="Risk from automated checks" value={filters.risk ?? ""} onChange={set("risk")} options={facetOptions(facets, "risk_level", filters.risk, titleCase)} />
          <Select label="Missing field" value={filters.missing ?? ""} onChange={set("missing")} options={facetOptions(facets, "missing_field", filters.missing, fieldLabel)} />
          <Select label="Origin" value={filters.origin ?? ""} onChange={set("origin")} options={facetOptions(facets, "origin", filters.origin, titleCase)} />
          {filters.source_key ? (
            <div className="flex flex-col justify-end gap-1 text-caption font-medium text-ink-2">
              <span>Source</span>
              <span className="flex items-center gap-2">
                <span className="mono min-w-0 truncate text-ink" title={filters.source_key}>
                  {filters.source_key}
                </span>
                <Button size="sm" variant="quiet" onClick={() => setParams({ source_key: null, pos: null })}>
                  Clear
                </Button>
              </span>
            </div>
          ) : null}
        </div>
      </Disclosure>

      {list.status === "error" ? (
        <ErrorState title="The review queue could not be loaded" message={list.error ?? ""} onRetry={list.reload} />
      ) : !data ? (
        <TableSkeleton rows={10} columns={6} label="Loading the review queue…" />
      ) : (
        <div ref={tableRef}>
          <DataTable
            label="Evidence awaiting or under review"
            columns={columns}
            rows={rows}
            rowKey={(r) => `${r.evidence_type}:${r.evidence_key}`}
            selectedKey={cursor >= 0 && rows[cursor] ? `${rows[cursor].evidence_type}:${rows[cursor].evidence_key}` : null}
            refreshing={list.refreshing}
            minWidth={1080}
            density="compact"
            toolbar={
              <>
                <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span>
                    <span className="num font-semibold text-ink">{formatCount(total)}</span> {total === 1 ? "record" : "records"}
                    {data.all_rows !== undefined && data.all_rows > total ? <span className="text-ink-3"> of {formatCount(data.all_rows)} reviewable</span> : null}
                  </span>
                  <span className="text-caption text-ink-3">
                    <Kbd>j</Kbd> <Kbd>k</Kbd> move · <Kbd>Enter</Kbd> open · <Kbd>?</Kbd> all shortcuts
                  </span>
                </p>
                <div className="flex flex-wrap items-end gap-2">
                  <Select label="Sort" value={filters.sort ?? "status"} onChange={(v) => setParams({ sort: v === "status" ? null : v, pos: null })} options={SORTS.map((s) => ({ value: s.value, label: s.label }))} className="w-56" />
                  <Select label="Order" value={filters.desc === "1" ? "1" : ""} onChange={(v) => setParams({ desc: v || null, pos: null })} options={[{ value: "", label: "As listed" }, { value: "1", label: "Reversed" }]} className="w-32" />
                  {anyFilter ? (
                    <Button
                      variant="secondary"
                      size="sm"
                      Icon={FilterX}
                      onClick={() => {
                        setQ("");
                        setParams(Object.fromEntries(["q", "etype", "status", "drug", "nct", "source_type", "source_key", "tier", "category", "risk", "missing", "origin", "changed", "pos"].map((k) => [k, null])));
                      }}
                    >
                      Clear filters
                    </Button>
                  ) : null}
                </div>
              </>
            }
            empty={
              <EmptyState title="No record matches these filters" className="m-3 border-0">
                Change or clear the filters to see more of the queue.
              </EmptyState>
            }
            footer={<Pagination page={page} pageSize={size} total={total} unfiltered={data.all_rows} onPage={(p) => setParams({ pos: p > 0 ? String(p * size) : null })} onPageSize={(s) => setParams({ size: s === PAGE_SIZES[0] ? null : String(s), pos: null })} pageSizes={PAGE_SIZES} noun="records" />}
          />
        </div>
      )}
      {/* The open action for the highlighted row, also reachable without the keyboard shortcut. */}
      {cursor >= 0 && rows[cursor] ? (
        <p className="mt-2 text-caption text-ink-3">
          Highlighted: <span className="text-ink-2">{rows[cursor].label}</span>.{" "}
          <button type="button" className="font-medium text-accent hover:underline" onClick={() => onOpen(rows[cursor], pos)}>
            Open it
          </button>
        </p>
      ) : null}
    </section>
  );
}
