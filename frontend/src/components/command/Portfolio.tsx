"use client";

import Link from "next/link";
import { type Column, DataTable, Missing, RegulatoryBadge, Skeleton, Term, TierBadge, VerificationBadge } from "@/components/ui";
import { phaseLabel, withQuery } from "@/lib/api";
import { formatCount } from "@/lib/format";
import type { ApiState } from "@/lib/api";
import type { WholeList } from "@/lib/paging";
import type { DrugSummary, StudyListItem } from "@/lib/types";
import { type DevelopmentSummary, developmentByDrug } from "./trialCounts";

/**
 * The portfolio by molecule: what it is (mechanism, derived from sourced receptor actions), where it stands with
 * the regulator (including a status withheld in review, with the review state of the statement it rests on), and how
 * far its tracked development has gone. The regulatory status is exactly what the API returns; the development stage
 * is derived here from the tracked trials and is labelled as such.
 */
export function Portfolio({ drugs, studies }: { drugs: DrugSummary[]; studies: ApiState<WholeList<StudyListItem, Record<string, unknown>>> }) {
  const development = studies.data ? developmentByDrug(studies.data.rows) : null;

  const stage = (d: DrugSummary) => {
    if (studies.status === "error") return <Missing>Not derived: the trial list did not load</Missing>;
    if (!development) return <Skeleton className="h-4 w-24" />;
    const dev: DevelopmentSummary | undefined = development.get(d.slug);
    if (!dev || dev.asStudyDrug === 0) return <Missing>No tracked trial as study drug</Missing>;
    return (
      <>
        <span className="font-medium text-ink">{dev.phase ? phaseLabel(dev.phase) : "Phase not reported"}</span>
        <span className="block text-caption text-ink-3">
          {formatCount(dev.ongoing)} ongoing, {formatCount(dev.completed)} completed
        </span>
      </>
    );
  };

  const columns: Column<DrugSummary>[] = [
    {
      key: "molecule",
      header: "Molecule and mechanism (derived)",
      rowHeader: true,
      className: "min-w-[150px] sm:min-w-[230px]",
      cell: (d) => {
        const code = d.code_name ?? d.brand_names?.join(" / ");
        return (
          <>
            <Link href={`/molecules?drug=${d.slug}`} className="font-semibold">
              {d.name}
            </Link>
            {code ? (
              <span className="mono ml-2 text-ink-3" translate="no">
                {code}
              </span>
            ) : null}
            {/* The mechanism belongs to the molecule's identity, so it sits with the name rather than in a far column. */}
            <span className="mt-0.5 block text-caption text-ink-2">{d.classification.class_label}</span>
            {d.tier_group === "ADJACENT" ? <span className="block text-caption text-ink-3">Adjacent pipeline</span> : null}
          </>
        );
      },
    },
    {
      key: "status",
      header: "Regulatory status",
      className: "min-w-[250px]",
      cell: (d) => (
        <span className="flex flex-col items-start gap-1">
          <RegulatoryBadge status={d.regulatory_status} available={d.regulatory_status_available} note={d.regulatory_status_note} />
          {d.regulatory_status_available === false ? null : d.regulatory_source ? (
            <span className="flex flex-wrap items-center gap-1">
              <TierBadge tier={d.regulatory_source.tier} label={d.regulatory_source.tier_label} />
              {d.regulatory_statement_status ? <VerificationBadge status={d.regulatory_statement_status} compact /> : null}
            </span>
          ) : (
            <Missing>Source not recorded</Missing>
          )}
          {d.regulatory_status_available === false ? null : <span className="num whitespace-nowrap text-caption text-ink-3">as of {d.regulatory_as_of ?? "date not recorded"}</span>}
        </span>
      ),
    },
    {
      key: "stage",
      header: (
        <Term k="tracked-development-stage">Most advanced tracked trial</Term>
      ),
      className: "min-w-[150px]",
      cell: stage,
    },
    {
      key: "trials",
      header: "Trials tracked",
      align: "right",
      cell: (d) => (
        <Link href={withQuery("/trials", { drug: d.slug })} className="num font-medium" aria-label={`${formatCount(d.n_studies)} tracked trials with ${d.name}: open in the trial atlas`}>
          {formatCount(d.n_studies)}
        </Link>
      ),
    },
  ];

  return (
    <>
      <p className="mb-3 max-w-[72ch] text-data text-ink-3">
        FDA-approved only when FDA labeling is on record. A status is withheld when the statement it rests on is rejected or held in review; the chip beside its source says how
        far that statement has been checked.
      </p>
      <DataTable
        label="Portfolio molecules"
        columns={columns}
        rows={drugs}
        rowKey={(d) => d.slug}
        maxHeight="none"
        pinFirstColumn
        minWidth={680}
        density="compact"
        empty={<p className="px-3 py-5 text-ink-3">No molecule is on record.</p>}
        footer={
          <p className="num text-data text-ink-2">
            {formatCount(drugs.length)} of {formatCount(drugs.length)} tracked molecules. Comparator drugs are listed in the molecule explorer.
          </p>
        }
      />
    </>
  );
}
