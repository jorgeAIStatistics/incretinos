"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { type Column, DataTable, type Density, EmptyState, ErrorState, Missing, PageHeader, Pagination, SearchField, SegmentedControl, Select, TableSkeleton, TabPanel, Tabs, TierBadge, useSort } from "@/components/ui";
import { diabetesLabel, phaseLabel, useApi, useDebounced, withQuery } from "@/lib/api";
import { isTier, TIERS } from "@/lib/evidence";
import { formatCount, trialStatusLabel } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { rowsOf, useClientPaging, usePagedList, useWholeList } from "@/lib/paging";
import type { DiscoveredTrial, DrugSummary, StudyListItem } from "@/lib/types";

type DepthKey = "trial" | "molecule" | "phase" | "status" | "population" | "enrolled" | "tiers" | "publication" | "completion";

const DEPTH_COLUMNS: Column<StudyListItem, DepthKey>[] = [
  {
    key: "trial",
    header: "Trial",
    rowHeader: true,
    className: "min-w-[140px] sm:min-w-[170px]",
    sortValue: (s) => s.display_name,
    cell: (s) => (
      <>
        <Link href={`/trials/${s.nct_id}`} className="font-semibold">
          {s.display_name}
        </Link>
        <span className="mono block text-ink-3" translate="no">
          {s.nct_id}
        </span>
      </>
    ),
  },
  {
    key: "molecule",
    header: "Molecule",
    className: "min-w-[120px]",
    cell: (s) => {
      const studied = s.drugs.filter((d) => d.role !== "COMPARATOR");
      const comparators = s.drugs.filter((d) => d.role === "COMPARATOR");
      return (
        <>
          {studied.length ? studied.map((d) => d.name).join(", ") : <Missing />}
          {comparators.length ? <span className="block text-caption text-ink-3">vs {comparators.map((d) => d.name).join(", ")}</span> : null}
        </>
      );
    },
  },
  { key: "phase", header: "Phase", className: "whitespace-nowrap", sortValue: (s) => s.phase, cell: (s) => phaseLabel(s.phase) },
  { key: "status", header: "Status", className: "min-w-[96px]", sortValue: (s) => s.overall_status, cell: (s) => trialStatusLabel(s.overall_status) },
  { key: "population", header: "Population", className: "min-w-[110px]", cell: (s) => diabetesLabel(s.diabetes_status) },
  {
    key: "enrolled",
    header: "Enrolled",
    align: "right",
    sortValue: (s) => s.enrollment,
    cell: (s) =>
      s.enrollment === null || s.enrollment === undefined ? (
        <Missing />
      ) : (
        <>
          {formatCount(s.enrollment)}
          {s.enrollment_type && s.enrollment_type !== "ACTUAL" ? <span className="block text-caption font-normal text-ink-3">{s.enrollment_type.toLowerCase()}</span> : null}
        </>
      ),
  },
  {
    key: "tiers",
    header: "Evidence tiers on record",
    className: "min-w-[236px]",
    cell: (s) =>
      s.evidence_tiers.length ? (
        <span className="flex flex-wrap gap-1">
          {s.evidence_tiers.map((t) => (
            <TierBadge key={t} tier={t} />
          ))}
        </span>
      ) : (
        <Missing>No results on record</Missing>
      ),
  },
  {
    key: "publication",
    header: "Primary publication",
    className: "min-w-[140px]",
    cell: (s) =>
      s.primary_publication ? (
        <>
          {s.primary_publication.journal} {s.primary_publication.year}
          <span className="mono block text-ink-3">PMID {s.primary_publication.pmid}</span>
        </>
      ) : (
        <Missing>None linked</Missing>
      ),
  },
  {
    key: "completion",
    header: "Primary completion",
    className: "whitespace-nowrap",
    sortValue: (s) => s.primary_completion_date,
    cell: (s) =>
      s.primary_completion_date ? (
        <>
          {s.primary_completion_date}
          {s.primary_completion_type && s.primary_completion_type !== "ACTUAL" ? <span className="block text-caption text-ink-3">{s.primary_completion_type.toLowerCase()}</span> : null}
        </>
      ) : (
        <Missing />
      ),
  },
];

const INDEX_COLUMNS: Column<DiscoveredTrial>[] = [
  {
    key: "nct",
    header: "NCT",
    rowHeader: true,
    className: "whitespace-nowrap",
    cell: (t) => (
      <a href={`https://clinicaltrials.gov/study/${t.nct_id}`} target="_blank" rel="noopener noreferrer" aria-label={`${t.nct_id} on ClinicalTrials.gov (opens in a new tab)`} className="mono" translate="no">
        {t.nct_id}
      </a>
    ),
  },
  {
    key: "title",
    header: "Title",
    className: "min-w-[280px] max-w-xl",
    cell: (t) => (
      <>
        {t.acronym ? <span className="font-semibold">{t.acronym}: </span> : null}
        {t.brief_title}
      </>
    ),
  },
  { key: "phase", header: "Phase", className: "whitespace-nowrap", cell: (t) => phaseLabel(t.phase) },
  { key: "status", header: "Status", className: "min-w-[110px]", cell: (t) => trialStatusLabel(t.overall_status) },
  { key: "results", header: "Results posted", cell: (t) => (t.has_results ? "Yes" : "No") },
  { key: "completion", header: "Primary completion", className: "whitespace-nowrap", cell: (t) => t.primary_completion_date ?? <Missing /> },
  { key: "depth", header: "Depth", className: "whitespace-nowrap", cell: (t) => (t.ingested_in_depth ? <Link href={`/trials/${t.nct_id}`}>In depth</Link> : <span className="text-ink-3">Index only</span>) },
];

export function TrialAtlas() {
  const [param, setParams] = useQueryParams();
  const drug = param("drug");
  const evidence = param("evidence");
  const tab = param("tab") === "index" ? "index" : "depth";
  const density: Density = param("density") === "compact" ? "compact" : "comfortable";
  const [q, setQ] = useState(param("q"));
  const query = useDebounced(q.trim(), 250);
  // The address follows the search box once typing pauses.
  useEffect(() => setParams({ q: query }), [query, setParams]);

  const studies = useWholeList<StudyListItem>("/studies");
  const drugs = useApi<unknown>("/drugs");
  const index = usePagedList<DiscoveredTrial, { note?: string }>(withQuery("/discovered", { drug, q: query }), { pageSize: 50, legacyLimit: 1000 });

  const all = studies.data?.rows;
  // The evidence filter offers exactly the tiers that occur in the data, so it cannot go stale.
  const tierOptions = useMemo(() => Array.from(new Set((all ?? []).flatMap((s) => s.evidence_tiers))).sort(), [all]);
  const filtered = useMemo(
    () =>
      (all ?? []).filter(
        (s) =>
          (!drug || s.drugs.some((d) => d.slug === drug)) &&
          (!query || `${s.display_name} ${s.brief_title} ${s.nct_id}`.toLowerCase().includes(query.toLowerCase())) &&
          (!evidence || (evidence === "none" ? s.evidence_tiers.length === 0 : s.evidence_tiers.includes(evidence))),
      ),
    [all, drug, query, evidence],
  );
  const { sort, setSort, sorted } = useSort(filtered, DEPTH_COLUMNS);
  const paging = useClientPaging(sorted, { pageSize: 50, resetKey: `${drug}|${evidence}|${query}|${sort?.key}|${sort?.direction}` });

  const filtersActive = Boolean(drug || evidence || query);
  const clear = () => {
    setQ("");
    setParams({ drug: null, evidence: null, q: null });
  };

  return (
    <>
      <PageHeader
        eyebrow="Clinical trial atlas"
        title="Every tracked trial, with the tier of evidence behind it"
        lede="Trials ingested in depth carry protocol, posted results, adverse events and linked publications. The registry index lists every Lilly-led trial matching the tracked agents."
      />
      <form role="search" aria-label="Filter trials" onSubmit={(e) => e.preventDefault()} className="mb-5 flex flex-wrap items-end gap-3">
        <Select
          label="Molecule"
          name="drug"
          value={drug}
          onChange={(v) => setParams({ drug: v })}
          options={[{ value: "", label: "All molecules" }, ...rowsOf<DrugSummary>(drugs.data).map((d) => ({ value: d.slug, label: d.name }))]}
          hint={drugs.status === "error" ? "The molecule list could not be loaded." : undefined}
          className="w-full sm:w-56"
        />
        <Select
          label="Evidence on record"
          name="evidence"
          value={evidence}
          onChange={(v) => setParams({ evidence: v })}
          options={[{ value: "", label: "Any" }, ...tierOptions.map((t) => ({ value: t, label: isTier(t) ? TIERS[t].label : t })), { value: "none", label: "No results on record" }]}
          className="w-full sm:w-64"
        />
        <SearchField
          label="Search"
          value={q}
          onChange={setQ}
          placeholder="Name, title or NCT number…"
          className="w-full sm:w-64"
        />
        {filtersActive ? (
          <button type="button" onClick={clear} className="h-(--control-h) rounded-control px-2 text-data font-medium text-accent hover:underline">
            Clear filters
          </button>
        ) : null}
      </form>

      <Tabs
        label="Trial sets"
        value={tab}
        onValueChange={(v) => setParams({ tab: v === "depth" ? null : v })}
        items={[
          { value: "depth", label: "Ingested in depth", count: studies.data ? formatCount(filtered.length) : undefined },
          { value: "index", label: "Registry index", count: index.data ? formatCount(index.data.total) : undefined },
        ]}
      >
        <TabPanel value="depth">
          {studies.status === "error" ? (
            <ErrorState title="The trial list could not be loaded" message={studies.error ?? ""} onRetry={studies.reload} />
          ) : !studies.data ? (
            <TableSkeleton rows={10} columns={7} label="Loading trials…" />
          ) : (
            <DataTable
              label="Trials ingested in depth"
              columns={DEPTH_COLUMNS}
              rows={paging.rows}
              rowKey={(s) => s.nct_id}
              sort={sort}
              onSort={setSort}
              density={density}
              pinFirstColumn
              minWidth={1060}
              toolbar={
                <>
                  <p>
                    <span className="num font-semibold text-ink">{formatCount(filtered.length)}</span> of {formatCount(studies.data.total)} trials{filtersActive ? " match the filters" : ""}. Sort by a column heading.
                  </p>
                  <SegmentedControl
                    label="Row density"
                    hideLabel
                    size="sm"
                    value={density}
                    onChange={(v) => setParams({ density: v === "comfortable" ? null : v })}
                    options={[
                      { value: "comfortable", label: "Comfortable" },
                      { value: "compact", label: "Compact" },
                    ]}
                  />
                </>
              }
              empty={
                <EmptyState title={filtersActive ? "No trial matches these filters" : "No trial has been ingested in depth"} className="m-3 border-0" action={filtersActive ? <button type="button" onClick={clear} className="rounded-chip font-medium text-accent hover:underline">Clear filters</button> : undefined}>
                  {filtersActive ? "Try a different molecule, evidence tier or search term. The registry index tab lists trials that are tracked by summary only." : "Run an ingestion to load trials."}
                </EmptyState>
              }
              footer={<Pagination page={paging.page} pageSize={paging.pageSize} total={paging.total} onPage={paging.setPage} onPageSize={paging.setPageSize} noun="trials" unfiltered={studies.data.total} truncated={studies.data.truncated} />}
            />
          )}
        </TabPanel>
        <TabPanel value="index">
          {index.status === "error" ? (
            <ErrorState title="The registry index could not be loaded" message={index.error ?? ""} onRetry={index.reload} />
          ) : !index.data ? (
            <TableSkeleton rows={10} columns={6} label="Loading the registry index…" />
          ) : (
            <>
              {index.data.extra.note ? <p className="mb-2 max-w-[80ch] text-data text-ink-3">{index.data.extra.note}</p> : null}
              <DataTable
                label="Registry index of tracked trials"
                columns={INDEX_COLUMNS}
                rows={index.data.rows}
                rowKey={(t) => t.nct_id}
                refreshing={index.refreshing}
                pinFirstColumn
                minWidth={860}
                empty={<EmptyState title="No registry record matches" className="m-3 border-0">Try a different molecule or search term.</EmptyState>}
                footer={<Pagination page={index.page} pageSize={index.pageSize} total={index.data.total} reachable={index.data.reachable} truncated={index.data.truncated} onPage={index.setPage} onPageSize={index.setPageSize} noun="registry records" />}
              />
            </>
          )}
        </TabPanel>
      </Tabs>
    </>
  );
}
