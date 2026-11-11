"use client";

import Link from "next/link";
import { ErrorState, Missing, NextLink, TableCard, TableSkeleton, Term, Timeline, TimelineItem } from "@/components/ui";
import { type ApiState, phaseLabel, withQuery } from "@/lib/api";
import { formatCount, trialStatusLabel } from "@/lib/format";
import type { WholeList } from "@/lib/paging";
import type { StudyBrief, StudyListItem } from "@/lib/types";
import { filterValue, ONGOING, phaseStatusMatrix } from "./trialCounts";

const phaseName = (p: string) => (p ? phaseLabel(p) : "Phase not reported");
const statusName = (s: string) => (s ? trialStatusLabel(s) : "Status not reported");

/** A count that opens the trial atlas filtered to exactly the trials it counts. A zero is plain text. */
function CountLink({ n, href, name }: { n: number; href: string; name: string }) {
  if (n === 0) return <span className="text-ink-3">0</span>;
  return (
    <Link href={href} className="font-medium" aria-label={`${formatCount(n)} ${name}: open in the trial atlas`}>
      {formatCount(n)}
    </Link>
  );
}

/** Tracked trials by registry phase and recruitment status. It is the data itself, not a chart, so no second view is needed. */
function PhaseStatusTable({ studies }: { studies: StudyListItem[] }) {
  const m = phaseStatusMatrix(studies);
  return (
    <TableCard label="Tracked trials by phase and status" maxHeight="none">
      <table className="tbl tbl-compact" style={{ minWidth: 560 }} data-testid="phase-status">
        <caption className="sr-only">Tracked trials by registry phase and recruitment status, with the number that have results posted to the registry</caption>
        <thead>
          <tr>
            <th scope="col">Phase</th>
            {m.statuses.map((s) => (
              <th key={s} scope="col" className="r">
                {statusName(s)}
              </th>
            ))}
            <th scope="col" className="r">
              All
            </th>
            <th scope="col" className="r">
              Results posted
            </th>
          </tr>
        </thead>
        <tbody>
          {m.phases.map((p) => (
            <tr key={p}>
              <th scope="row" className="whitespace-nowrap font-medium text-ink">
                {phaseName(p)}
              </th>
              {m.statuses.map((s) => (
                <td key={s} className="r num">
                  <CountLink n={m.cells.get(`${p}|${s}`) ?? 0} href={withQuery("/trials", { phase: filterValue(p), status: filterValue(s) })} name={`${phaseName(p)} trials, ${statusName(s).toLowerCase()}`} />
                </td>
              ))}
              <td className="r num font-semibold">
                <CountLink n={m.byPhase.get(p) ?? 0} href={withQuery("/trials", { phase: filterValue(p) })} name={`${phaseName(p)} trials`} />
              </td>
              <td className="r num">
                <CountLink n={m.resultsByPhase.get(p) ?? 0} href={withQuery("/trials", { phase: filterValue(p), results: "yes" })} name={`${phaseName(p)} trials with results posted`} />
              </td>
            </tr>
          ))}
          <tr>
            <th scope="row" className="font-semibold text-ink">
              All phases
            </th>
            {m.statuses.map((s) => (
              <td key={s} className="r num font-semibold">
                <CountLink n={m.byStatus.get(s) ?? 0} href={withQuery("/trials", { status: filterValue(s) })} name={`trials, ${statusName(s).toLowerCase()}`} />
              </td>
            ))}
            <td className="r num font-semibold">
              <CountLink n={m.total} href="/trials" name="tracked trials" />
            </td>
            <td className="r num font-semibold">
              <CountLink n={m.results} href={withQuery("/trials", { results: "yes" })} name="trials with results posted" />
            </td>
          </tr>
        </tbody>
      </table>
    </TableCard>
  );
}

function TrialLine({ s }: { s: StudyBrief }) {
  return (
    <>
      <Link href={`/trials/${s.nct_id}`} className="font-medium">
        {s.display_name}
      </Link>
      <span className="text-ink-3">
        {" "}
        {phaseLabel(s.phase)}, {trialStatusLabel(s.overall_status).toLowerCase()}
        {s.drugs.length ? `; ${s.drugs.map((d) => d.name).join(" + ")}` : ""}
      </span>
    </>
  );
}

export function Development({ studies, upcoming, completedWithoutResults }: { studies: ApiState<WholeList<StudyListItem, Record<string, unknown>>>; upcoming: StudyBrief[]; completedWithoutResults: StudyBrief[] }) {
  // The overview lists the earliest dates only; the full number comes from the trial list by the same rule.
  const upcomingTotal = studies.data ? studies.data.rows.filter((s) => !s.has_results && ["RECRUITING", "ACTIVE_NOT_RECRUITING"].includes(s.overall_status ?? "") && s.primary_completion_date).length : null;
  const ongoing = studies.data ? studies.data.rows.filter((s) => ONGOING.has(s.overall_status ?? "")).length : null;
  return (
    <div className="grid grid-cols-1 gap-x-8 gap-y-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
      <div className="min-w-0">
        <h3 className="text-body font-semibold text-ink">Tracked trials by phase and status</h3>
        <p className="mb-2 mt-0.5 text-data text-ink-3">
          Registry phase and recruitment status of every trial ingested in depth{ongoing !== null ? `; ${formatCount(ongoing)} are still running` : ""}. Each count opens the trial atlas filtered to those trials.
        </p>
        {studies.status === "error" ? (
          <ErrorState title="The trial list could not be loaded" message={studies.error ?? ""} onRetry={studies.reload} />
        ) : !studies.data ? (
          <TableSkeleton rows={4} columns={6} label="Loading the trial list…" />
        ) : (
          <PhaseStatusTable studies={studies.data.rows} />
        )}

        <h3 className="mt-6 text-body font-semibold text-ink">
          Completed, no results posted to the{" "}
          <Term k="registry-posted-results">registry</Term>
        </h3>
        <p className="mb-2 mt-0.5 text-data text-ink-3">Results may exist in a publication or a press release; none are posted on ClinicalTrials.gov.</p>
        {completedWithoutResults.length === 0 ? (
          <p className="text-data text-ink-3">Every completed tracked trial has results posted to the registry.</p>
        ) : (
          <>
            <ul className="grid grid-cols-1 gap-x-6 gap-y-1.5 text-data sm:grid-cols-2" data-testid="completed-without-results">
              {completedWithoutResults.map((s) => (
                <li key={s.nct_id} className="min-w-0">
                  <Link href={`/trials/${s.nct_id}`} className="font-medium">
                    {s.display_name}
                  </Link>
                  <span className="text-ink-3">
                    {" "}
                    {phaseLabel(s.phase)}, primary completion{" "}
                    {s.primary_completion_date ? <span className="num">{s.primary_completion_date}</span> : <Missing>not reported</Missing>}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-2">
              <NextLink href={withQuery("/trials", { status: "COMPLETED", results: "no" })}>
                All {formatCount(completedWithoutResults.length)} in the trial atlas
              </NextLink>
            </p>
          </>
        )}
      </div>

      <div className="min-w-0">
        <h3 className="text-body font-semibold text-ink">
          <Term k="primary-completion-date">Primary completions</Term> of running trials without posted results
        </h3>
        <p className="mb-3 mt-0.5 text-data text-ink-3">
          Recruiting or active trials, earliest date first, as the registry states it. An actual date means the registry reports the primary completion as reached.
          {upcomingTotal !== null ? ` ${formatCount(upcoming.length)} earliest of ${formatCount(upcomingTotal)}.` : ""}
        </p>
        {upcoming.length === 0 ? (
          <p className="text-data text-ink-3">No running trial without posted results has a primary completion date on record.</p>
        ) : (
          <Timeline label="Primary completions of running trials without posted results">
            {upcoming.map((s) => (
              <TimelineItem key={s.nct_id} date={s.primary_completion_date} dateNote={s.primary_completion_type ? s.primary_completion_type.toLowerCase() : "type not reported"} testId="upcoming-completion">
                <TrialLine s={s} />
              </TimelineItem>
            ))}
          </Timeline>
        )}
      </div>
    </div>
  );
}
