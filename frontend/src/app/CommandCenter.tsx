"use client";

import { ArrowRight } from "lucide-react";
import type { ReactNode } from "react";
import { Attention } from "@/components/command/Attention";
import { Development } from "@/components/command/Development";
import { Portfolio } from "@/components/command/Portfolio";
import { Quality } from "@/components/command/Quality";
import { Changes, Comparisons, Gaps } from "@/components/command/Signals";
import type { ConflictListExtra } from "@/components/conflicts/types";
import { Async, ErrorState, Ledger, LedgerItem, LinkButton, NextLink, PageHeader, Skeleton } from "@/components/ui";
import { type ApiState, useApi } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { useWholeList } from "@/lib/paging";
import type { QualityOverview } from "@/lib/quality";
import type { ReviewSummary } from "@/lib/review";
import type { ChangeEvent, Overview, StudyListItem, ValidationReport } from "@/lib/types";

/*
 * Research Command Center. One reading order, top to bottom:
 *   1. what this is (header) and how much evidence is on record (ledger)
 *   2. the portfolio by molecule, beside the unresolved work that needs a person
 *   3. clinical development: trials by phase and status, completions, completed trials without posted results
 *   4. evidence quality, dimension by dimension (never one score)
 *   5. open evidence questions and recent source changes
 *   6. where to go next: the worked comparisons
 * Each block loads on its own: a slow or failed request affects only its block, never the page.
 */

function Heading({ id, title, hint, action }: { id: string; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
      <div className="min-w-0">
        <h2 id={id} className="text-title font-semibold text-ink">
          {title}
        </h2>
        {hint ? <p className="mt-0.5 max-w-[72ch] text-data text-ink-3">{hint}</p> : null}
      </div>
      {action ? <div className="shrink-0 text-data">{action}</div> : null}
    </div>
  );
}

/** A request that has answered, with data or with an error. */
const settled = (state: ApiState<unknown>): boolean => state.status === "success" || state.status === "error";

function OverviewSkeleton() {
  return (
    <div role="status" aria-label="Loading the evidence overview…">
      <div className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="h-14" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
      <span className="sr-only">Loading the evidence overview…</span>
    </div>
  );
}

function Dashboard({ data }: { data: Overview }) {
  const m = data.meta;
  const studies = useWholeList<StudyListItem>("/studies");
  // The four heavy reads run one after another, most urgent first. Fired together they compete for the same
  // server and every one of them arrives late (measured: about 17 s together against about 3 s in sequence).
  const summary = useApi<ReviewSummary>("/review/summary");
  const conflicts = useApi<ConflictListExtra>(settled(summary) ? "/conflicts?limit=1" : null);
  const validation = useApi<ValidationReport>(settled(conflicts) ? "/validation" : null);
  const quality = useApi<QualityOverview>(settled(validation) ? "/quality" : null);
  const gapLabels = useApi<{ nature_labels?: Record<string, string> }>("/gaps?limit=1");
  const changes = useApi<{ total: number; rows: ChangeEvent[] }>("/changes");
  const openChanges = changes.data ? changes.data.rows.filter((c) => c.review_status === "OPEN").length : null;

  return (
    <>
      <section aria-labelledby="on-record" className="mb-9">
        <h2 id="on-record" className="sr-only">
          Evidence on record
        </h2>
        <Ledger label="Evidence on record">
          <LedgerItem label="Trials in depth" value={formatCount(m.counts.studies)} href="/trials" linkLabel={`${formatCount(m.counts.studies)} trials ingested in depth: open the trial atlas`} note={`${formatCount(m.counts.discovered_trials)} in the registry index`} testId="ledger-trials" />
          <LedgerItem label="Reported results" value={formatCount(m.counts.results)} note={`in analytic use; ${formatCount(m.counts.analyses)} between-arm analyses`} />
          <LedgerItem label="Adverse-event rows" value={formatCount(m.counts.adverse_event_rows)} />
          <LedgerItem label="Publications" value={formatCount(m.counts.publications)} />
          <LedgerItem label="Sources" value={formatCount(m.counts.sources)} href="/sources" linkLabel={`${formatCount(m.counts.sources)} sources: open Sources and Audit`} note="hashed and versioned" testId="ledger-sources" />
          <LedgerItem label="Open evidence questions" value={formatCount(m.counts.gaps)} href="/gaps" linkLabel={`${formatCount(m.counts.gaps)} open evidence questions: open the gap tracker`} note="each with the reason it is open" testId="ledger-gaps" />
        </Ledger>
      </section>

      <div className="mb-10 grid grid-cols-1 gap-x-10 gap-y-9 lg:grid-cols-[minmax(0,1fr)_21rem]">
        <section aria-labelledby="portfolio" className="min-w-0">
          <Heading id="portfolio" title="Portfolio" action={<NextLink href="/molecules">Molecule explorer</NextLink>} />
          <Portfolio drugs={data.drugs} studies={studies} />
        </section>
        <aside aria-labelledby="attention" className="min-w-0 lg:border-l lg:border-hairline lg:pl-8">
          <Heading id="attention" title="Needs attention" hint="Unresolved review work. Each count opens the list it counts." />
          <Attention summary={summary} conflicts={conflicts} queueItems={m.counts.open_review_items} validation={validation} />
        </aside>
      </div>

      <section aria-labelledby="development" className="mb-10">
        <Heading id="development" title="Clinical development" action={<NextLink href="/trials">Clinical trial atlas</NextLink>} />
        <Development studies={studies} upcoming={data.upcoming_readouts} completedWithoutResults={data.completed_without_registry_results} />
      </section>

      <section aria-labelledby="quality" className="mb-10">
        <Heading id="quality" title="Evidence quality" />
        <Quality meta={m} quality={quality} />
      </section>

      <div className="mb-10 grid grid-cols-1 gap-x-10 gap-y-9 lg:grid-cols-2">
        <section aria-labelledby="gaps" className="min-w-0">
          <Heading id="gaps" title="Open evidence questions" />
          <Gaps total={m.counts.gaps} byNature={data.gaps_by_nature} top={data.top_gaps} natureLabels={gapLabels.data?.nature_labels} />
        </section>
        <section aria-labelledby="changes" className="min-w-0">
          <Heading
            id="changes"
            title="Recent source changes"
            hint={openChanges !== null ? `${formatCount(openChanges)} of ${formatCount(changes.data?.total ?? 0)} detected changes await review.` : undefined}
            action={<NextLink href="/sources?tab=changes">Changes and review queue</NextLink>}
          />
          {changes.status === "error" ? <ErrorState title="The change count could not be loaded" message={changes.error ?? ""} onRetry={changes.reload} className="mb-3" /> : null}
          <Changes changes={data.changes} />
        </section>
      </div>

      <section aria-labelledby="next" className="mb-4">
        <Heading id="next" title="Worked comparisons" hint="Each opens in the comparison laboratory with its classification and the reasons for it." />
        <Comparisons presets={data.presets} />
      </section>
    </>
  );
}

export function CommandCenter() {
  const overview = useApi<Overview>("/overview");
  return (
    <>
      <PageHeader
        eyebrow="Research command center"
        title="What the public evidence supports, and what it does not"
        lede="Lilly's incretin and amylin portfolio, read from registries, publications, FDA labeling and sponsor disclosures. Every number carries its source, estimand and verification status."
      >
        <LinkButton href="/compare">
          Open the comparison laboratory <ArrowRight aria-hidden size={15} />
        </LinkButton>
      </PageHeader>
      <Async state={overview} errorTitle="The evidence overview could not be loaded" loading={<OverviewSkeleton />}>
        {(data) => <Dashboard data={data} />}
      </Async>
    </>
  );
}
