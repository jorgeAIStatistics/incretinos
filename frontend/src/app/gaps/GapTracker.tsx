"use client";

import clsx from "clsx";
import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Callout, type Column, DataTable, EmptyState, ErrorState, Meter, PageHeader, Pagination, Panel, Select, Skeleton, Slider, TableSkeleton, Term, type Tone } from "@/components/ui";
import { useApi, useDebounced, withQuery } from "@/lib/api";
import { enumLabel, formatCount } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { rowsOf, usePagedList } from "@/lib/paging";
import type { DrugSummary, Gap, GapListExtra } from "@/lib/types";

/** Presentation only: which tone and icon a reason gets. The wording itself comes from the API. */
const NATURE_TONE: Record<string, Tone> = { PUBLIC_REPORTING_GAP: "warn", EVIDENCE_IN_GENERATION: "info", NOT_KNOWN_TO_BE_STUDIED: "muted", METHODOLOGICAL: "muted", SOURCE_CONFLICT: "critical", RECORD_CHANGE: "warn" };

/** The API's reason labels read "Short name — explanation". The short name is the part before the dash. */
const shortReason = (label: string | null | undefined, code: string): string => (label ? label.split(" — ")[0] : enumLabel(code));

const WEIGHTS = [
  { key: "clinical_importance", param: "w_importance", label: "Clinical importance" },
  { key: "uncertainty", param: "w_uncertainty", label: "Uncertainty" },
  { key: "tractability", param: "w_tractability", label: "Tractability" },
  { key: "actionability", param: "w_actionability", label: "Actionable now" },
] as const;

type Weights = Record<string, number>;

function GapDetail({ gap }: { gap: Gap }) {
  const terms = Object.entries(gap.priority.components).map(([k, v]) => `${(gap.priority.weights[k] ?? 0).toFixed(2)} × ${v.toFixed(2)} (${k.replace(/_/g, " ")})`);
  return (
    <div className="max-w-5xl pl-0 sm:pl-9">
      <dl className="grid grid-cols-1 gap-x-8 gap-y-3 text-data md:grid-cols-2">
        {(
          [
            ["Evidence already available", gap.available_evidence],
            ["Missing evidence", gap.missing_evidence],
            ["Potential analytical approach", gap.analytic_approach],
            ["New data required", gap.data_required],
          ] as const
        ).map(([term, text]) => (
          <div key={term}>
            <dt className="font-semibold text-ink">{term}</dt>
            <dd className="text-ink-2">{text ?? <span className="text-ink-3">Not recorded</span>}</dd>
          </div>
        ))}
      </dl>
      {gap.gap_nature_label ? <p className="mt-3 text-data text-ink-2">{gap.gap_nature_label}</p> : null}
      <p className="num mt-1 text-data text-ink-3">
        Priority {gap.priority.score.toFixed(3)} = {terms.join(" + ")}
      </p>
      <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-data">
        {gap.related_ncts.map((n) => (
          <Link key={n} href={`/trials/${n}`} className="mono" translate="no">
            {n}
          </Link>
        ))}
        {gap.source_ids.map((id) => (
          <Link key={id} href={`/sources?id=${id}`}>
            Source {id}
          </Link>
        ))}
        <span className="text-ink-3">
          Detection rule <span className="mono" translate="no">{gap.rule_id}</span>
        </span>
      </p>
    </div>
  );
}

export function GapTracker() {
  const [param, setParams] = useQueryParams();
  const drug = param("drug");
  const nature = param("nature");
  // null means "use the service's default weights": the defaults are read from the response, not repeated here.
  const [weights, setWeights] = useState<Weights | null>(null);
  const applied = useDebounced(weights, 250);
  const [open, setOpen] = useState<number | null>(null);

  const base = withQuery("/gaps", { drug, nature, ...(applied ? Object.fromEntries(WEIGHTS.map((w) => [w.param, applied[w.key]])) : {}) });
  const gaps = usePagedList<Gap, GapListExtra>(base, { pageSize: 25 });
  const drugs = useApi<unknown>("/drugs");

  const extra = gaps.data?.extra;
  const shown: Weights | null = weights ?? extra?.weights ?? null;
  const allZero = shown !== null && WEIGHTS.every((w) => (shown[w.key] ?? 0) === 0);
  const natureLabels = extra?.nature_labels ?? {};
  const natureOptions = Object.entries(natureLabels).map(([value, label]) => ({ value, label: shortReason(label, value) }));
  if (nature && !natureLabels[nature]) natureOptions.push({ value: nature, label: enumLabel(nature) });
  const filtersActive = Boolean(drug || nature);

  const columns: Column<Gap>[] = [
    { key: "rank", header: "Rank", align: "right", className: "w-12 text-ink-3", cell: (_, i) => gaps.page * gaps.pageSize + i + 1 },
    {
      key: "question",
      header: "Open question",
      rowHeader: true,
      className: "min-w-[300px] max-w-xl",
      cell: (g) => (
        <>
          <button type="button" aria-expanded={open === g.id} onClick={() => setOpen(open === g.id ? null : g.id)} className="flex items-start gap-1.5 rounded-chip text-left font-medium text-ink transition-colors duration-(--dur-fast) hover:text-accent">
            <ChevronRight aria-hidden size={14} className={clsx("mt-[3px] shrink-0 text-ink-3 transition-transform duration-(--dur-base) ease-standard", open === g.id && "rotate-90")} />
            <span>{g.question}</span>
          </button>
          <span className="block pl-5 text-caption text-ink-3">
            {enumLabel(g.category)}
            {g.review_status !== "OPEN" ? `, ${enumLabel(g.review_status).toLowerCase()}` : ""}
          </span>
        </>
      ),
    },
    { key: "molecule", header: "Molecule", className: "min-w-[120px]", cell: (g) => g.drug?.name ?? <span className="text-ink-3">Not molecule-specific</span> },
    {
      key: "nature",
      header: "Why it is open",
      cell: (g) => (
        <Badge tone={NATURE_TONE[g.gap_nature] ?? "muted"} title={g.gap_nature_label ?? undefined}>
          {shortReason(g.gap_nature_label, g.gap_nature)}
        </Badge>
      ),
    },
    { key: "certainty", header: "Evidence certainty", className: "whitespace-nowrap", cell: (g) => enumLabel(g.evidence_certainty) },
    { key: "priority", header: "Priority", className: "whitespace-nowrap", cell: (g) => <Meter value={g.priority.score} label={`Priority score for: ${g.question}`} /> },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Evidence gap tracker"
        title="What the public record does not yet answer — and why"
        lede="Each gap states what is available, what is missing, and the reason it is open. A gap in public reporting is not confirmation that the sponsor has no data."
      />
      <form onSubmit={(e) => e.preventDefault()} className="mb-4 flex flex-wrap items-end gap-3">
        <Select
          label="Molecule"
          name="drug"
          value={drug}
          onChange={(v) => {
            setOpen(null);
            setParams({ drug: v });
          }}
          options={[{ value: "", label: "All molecules" }, ...rowsOf<DrugSummary>(drugs.data).filter((d) => d.tier_group !== "COMPARATOR").map((d) => ({ value: d.slug, label: d.name }))]}
          hint={drugs.status === "error" ? "The molecule list could not be loaded." : undefined}
          className="w-full sm:w-60"
        />
        <Select
          label="Why the question is open"
          name="nature"
          value={nature}
          onChange={(v) => {
            setOpen(null);
            setParams({ nature: v });
          }}
          options={[{ value: "", label: "Any reason" }, ...natureOptions]}
          className="w-full sm:w-72"
        />
        {filtersActive ? (
          <button type="button" onClick={() => setParams({ drug: null, nature: null })} className="h-(--control-h) rounded-control px-2 text-data font-medium text-accent hover:underline">
            Clear filters
          </button>
        ) : null}
      </form>

      <Panel as="section" className="mb-5">
        <div className="grid grid-cols-1 gap-x-8 gap-y-3 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]">
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-body font-semibold text-ink">Prioritization weights</h2>
              {weights ? (
                <Button variant="quiet" size="sm" onClick={() => setWeights(null)}>
                  Reset to defaults
                </Button>
              ) : null}
            </div>
            <p className="mt-0.5 text-data text-ink-3">
              The <Term k="priority-score">priority score</Term> is the weighted sum of four components, with the weights normalised to 1. Change them to match your programme.
            </p>
          </div>
          <div>
            {shown ? (
              <div className="grid grid-cols-2 gap-x-5 gap-y-2 md:grid-cols-4">
                {WEIGHTS.map((w) => (
                  <Slider key={w.key} label={w.label} name={w.param} value={shown[w.key] ?? 0} onChange={(v) => setWeights({ ...shown, [w.key]: v })} />
                ))}
              </div>
            ) : gaps.status === "error" ? (
              <p className="text-data text-ink-3">Weights are unavailable until the list loads.</p>
            ) : (
              <Skeleton className="h-12" />
            )}
            {allZero ? <p role="status" className="mt-2 text-caption font-medium text-warn">All four weights are zero, so every score is 0 and the order carries no meaning. Raise at least one weight.</p> : null}
          </div>
        </div>
      </Panel>

      {gaps.status === "error" ? (
        <ErrorState title="The open questions could not be loaded" message={gaps.error ?? ""} onRetry={gaps.reload} />
      ) : !gaps.data ? (
        <TableSkeleton rows={8} columns={5} label="Loading open questions…" />
      ) : (
        <DataTable
          label="Open evidence questions, highest priority first"
          columns={columns}
          rows={gaps.data.rows}
          rowKey={(g) => g.id}
          expandedKey={open}
          renderExpanded={(g) => <GapDetail gap={g} />}
          refreshing={gaps.refreshing}
          minWidth={880}
          toolbar={<p><span className="num font-semibold text-ink">{formatCount(gaps.data.total)}</span> open {gaps.data.total === 1 ? "question" : "questions"}, highest priority first. Select a question to see what is available and what is missing.</p>}
          empty={
            <EmptyState title={filtersActive ? "No open question matches these filters" : "No open question is recorded"} className="m-3 border-0">
              {filtersActive ? "Choose another molecule or reason, or clear the filters." : "Gaps are detected by rule at each ingestion."}
            </EmptyState>
          }
          footer={<Pagination page={gaps.page} pageSize={gaps.pageSize} total={gaps.data.total} reachable={gaps.data.reachable} truncated={gaps.data.truncated} onPage={(p) => { setOpen(null); gaps.setPage(p); }} onPageSize={gaps.setPageSize} noun="open questions" />}
        />
      )}
      {extra?.note ? (
        <Callout tone="muted" className="mt-4">
          {extra.note}
        </Callout>
      ) : null}
    </>
  );
}
