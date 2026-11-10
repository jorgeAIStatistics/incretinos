"use client";

import clsx from "clsx";
import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef } from "react";
import { Callout, type Column, DataTable, EmptyState, ErrorState, PageHeader, Pagination, Select, TableSkeleton, TierBadge } from "@/components/ui";
import { withQuery } from "@/lib/api";
import { enumLabel, formatCount } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { usePagedList } from "@/lib/paging";
import { CLASS_LOOK, CLASS_ORDER, ClassBadge } from "./ClassBadge";
import { ConflictInspector } from "./ConflictInspector";
import type { ConflictClass, ConflictListExtra, ConflictSummary } from "./types";

/*
 * /conflicts: every group of rows from different public sources that purport to report the same quantity, classified
 * by the API. Filters and the open group live in the address, so a link from the review workbench lands on the group.
 */

const minus = (text: string): string => text.replace(/^-(?=\d)/, "−");
const ESTIMAND_SHORT: Record<string, string> = { EFFICACY: "efficacy", TREATMENT_REGIMEN: "treatment-regimen", OTHER: "other estimand", NOT_STATED: "estimand not named" };

function ClassFilter({ value, counts, labels, definitions, onChange }: { value: string; counts: Record<ConflictClass, number> | undefined; labels: Record<ConflictClass, string> | undefined; definitions: Record<ConflictClass, string> | undefined; onChange: (v: string) => void }) {
  const total = counts ? CLASS_ORDER.reduce((sum, c) => sum + (counts[c] ?? 0), 0) : null;
  const options: { key: string; label: string; n: number | null; Icon?: (typeof CLASS_LOOK)[ConflictClass]["Icon"]; hint?: string }[] = [
    { key: "", label: "All classes", n: total },
    ...CLASS_ORDER.map((c) => ({ key: c, label: labels?.[c] ?? CLASS_LOOK[c].fallback, n: counts ? (counts[c] ?? 0) : null, Icon: CLASS_LOOK[c].Icon, hint: definitions?.[c] })),
  ];
  return (
    <div role="group" aria-label="Filter by classification" className="mb-4 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 lg:grid-cols-5" data-testid="class-filter">
      {options.map((o) => {
        const selected = value === o.key;
        return (
          <button
            key={o.key || "all"}
            type="button"
            aria-pressed={selected}
            title={o.hint}
            onClick={() => onChange(o.key)}
            className={clsx(
              "flex min-h-(--control-h) min-w-0 items-center justify-between gap-2 rounded-control border px-3 py-2 text-left text-data transition-colors duration-(--dur-fast)",
              selected ? "border-accent bg-accent-soft font-semibold text-ink shadow-[inset_3px_0_0_var(--accent)]" : "border-control bg-surface text-ink-2 hover:bg-surface-hover",
            )}
          >
            <span className="flex min-w-0 items-center gap-1.5">
              {o.Icon ? <o.Icon aria-hidden size={14} className="shrink-0" /> : null}
              <span className="truncate">{o.label}</span>
            </span>
            <span className="num shrink-0 font-semibold text-ink" data-testid={`class-count-${o.key || "ALL"}`}>
              {o.n === null ? "…" : formatCount(o.n)}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function ConflictsView() {
  const [param, setParams] = useQueryParams();
  const cls = param("class");
  const drug = param("drug");
  const study = param("study");
  const category = param("category");
  const type = param("type");
  const group = param("group");
  const list = usePagedList<ConflictSummary, ConflictListExtra>(withQuery("/conflicts", { class: cls, drug, study, endpoint_category: category, evidence_type: type }), { pageSize: 25 });
  const top = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (group) top.current?.scrollIntoView({ block: "start" });
  }, [group]);

  const extra = list.data?.extra;
  const filtersActive = Boolean(cls || drug || study || category || type);

  const columns: Column<ConflictSummary>[] = [
    {
      key: "quantity",
      header: "Quantity",
      rowHeader: true,
      className: "min-w-[260px] max-w-md",
      cell: (g) => (
        <>
          <button type="button" onClick={() => setParams({ group: g.group_id })} className="flex items-start gap-1.5 rounded-chip text-left font-medium text-ink transition-colors duration-(--dur-fast) hover:text-accent" data-testid="open-group">
            <ChevronRight aria-hidden size={14} className="mt-[3px] shrink-0 text-ink-3" />
            <span>
              {g.study.display_name}: {g.quantity}
            </span>
          </button>
          <span className="block pl-5 text-caption text-ink-3">
            {g.arm}
            {g.time_point ? `, ${g.time_point}` : ""}. {g.evidence_type_label}.
          </span>
        </>
      ),
    },
    { key: "class", header: "Classification", className: "whitespace-nowrap", cell: (g) => <ClassBadge value={g.class} label={g.class_label} definition={extra?.class_definitions?.[g.class]} /> },
    {
      key: "values",
      header: "Values reported",
      className: "min-w-[260px]",
      cell: (g) => (
        <ul className="flex flex-col gap-1">
          {g.values.map((v, i) => (
            <li key={`${v.evidence_key ?? i}`} className="flex flex-wrap items-center gap-1.5">
              <span className="num font-semibold text-ink">
                {minus(v.value_printed)}
                {v.unit ? <span className="font-normal text-ink-3"> {v.unit}</span> : null}
              </span>
              <TierBadge tier={v.tier} explain={false} />
              {g.evidence_type !== "adverse_event" && v.estimand_label ? <span className="text-caption text-ink-3">{ESTIMAND_SHORT[v.estimand_label] ?? v.estimand_label}</span> : null}
            </li>
          ))}
        </ul>
      ),
    },
    { key: "sources", header: "Sources", align: "right", cell: (g) => formatCount(g.n_sources) },
  ];

  return (
    <div ref={top} className="scroll-mt-20">
      <PageHeader
        eyebrow="Review and provenance"
        title="Conflicts"
        lede="Where public sources report the same quantity, whether they agree — and when they do not, whether a stated difference (estimand, baseline, analysis set, follow-up, endpoint definition) explains it. No source wins because of its kind."
      />
      {group ? (
        <ConflictInspector groupId={group} onBack={() => setParams({ group: null })} />
      ) : (
        <>
          <ClassFilter value={cls} counts={extra?.counts} labels={extra?.class_labels} definitions={extra?.class_definitions} onChange={(v) => setParams({ class: v })} />
          <form onSubmit={(e) => e.preventDefault()} className="mb-4 flex flex-wrap items-end gap-3">
            <Select
              label="Molecule"
              name="drug"
              value={drug}
              onChange={(v) => setParams({ drug: v })}
              options={[{ value: "", label: "All molecules" }, ...(extra?.drugs ?? []).map((d) => ({ value: d, label: enumLabel(d) })), ...(drug && !(extra?.drugs ?? []).includes(drug) ? [{ value: drug, label: drug }] : [])]}
              className="w-full sm:w-52"
            />
            <Select
              label="Trial"
              name="study"
              value={study}
              onChange={(v) => setParams({ study: v })}
              options={[{ value: "", label: "All trials" }, ...(extra?.studies ?? []).map((s) => ({ value: s.nct_id, label: `${s.display_name} (${s.nct_id})` })), ...(study && !(extra?.studies ?? []).some((s) => s.nct_id === study) ? [{ value: study, label: study }] : [])]}
              className="w-full sm:w-72"
            />
            <Select
              label="Endpoint category"
              name="category"
              value={category}
              onChange={(v) => setParams({ category: v })}
              options={[{ value: "", label: "All categories" }, ...(extra?.categories ?? []).map((c) => ({ value: c, label: enumLabel(c) })), ...(category && !(extra?.categories ?? []).includes(category) ? [{ value: category, label: enumLabel(category) }] : [])]}
              className="w-full sm:w-60"
            />
            <Select
              label="Evidence type"
              name="type"
              value={type}
              onChange={(v) => setParams({ type: v })}
              options={[{ value: "", label: "All evidence types" }, ...Object.entries(extra?.evidence_type_labels ?? { result: "Arm-level result", analysis: "Between-arm analysis", adverse_event: "Adverse event" }).map(([value, label]) => ({ value, label }))]}
              className="w-full sm:w-52"
            />
            {filtersActive ? (
              <button type="button" onClick={() => setParams({ class: null, drug: null, study: null, category: null, type: null })} className="h-(--control-h) rounded-control px-2 text-data font-medium text-accent hover:underline">
                Clear filters
              </button>
            ) : null}
          </form>
          {list.status === "error" ? (
            <ErrorState title="The source groups could not be loaded" message={list.error ?? ""} onRetry={list.reload} />
          ) : !list.data ? (
            <TableSkeleton rows={8} columns={4} label="Loading source groups…" />
          ) : (
            <DataTable
              label="Groups of rows from several sources that report the same quantity"
              columns={columns}
              rows={list.data.rows}
              rowKey={(g) => g.group_id}
              refreshing={list.refreshing}
              minWidth={820}
              toolbar={
                <p>
                  <span className="num font-semibold text-ink">{formatCount(list.data.total)}</span> {list.data.total === 1 ? "group" : "groups"}
                  {extra?.all_groups !== undefined && filtersActive ? ` of ${formatCount(extra.all_groups)}` : ""}, most action-requiring first. Select a quantity to see its values side by side.
                </p>
              }
              empty={
                <EmptyState title={filtersActive ? "No group matches these filters" : "No quantity is reported by more than one source"} className="m-3 border-0">
                  {filtersActive ? "Choose another class or filter, or clear the filters." : "Groups appear when two sources report the same study, group, endpoint and time point."}
                </EmptyState>
              }
              footer={<Pagination page={list.page} pageSize={list.pageSize} total={list.data.total} reachable={list.data.reachable} truncated={list.data.truncated} onPage={list.setPage} onPageSize={list.setPageSize} noun="groups" />}
            />
          )}
          {extra?.note ? (
            <Callout tone="muted" className="mt-4">
              {extra.note} Unresolved true conflicts and unreconciled differences also appear in the <Link href="/gaps?nature=SOURCE_CONFLICT">Evidence Gap Tracker</Link>.
            </Callout>
          ) : null}
        </>
      )}
    </div>
  );
}
