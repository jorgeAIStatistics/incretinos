"use client";

import Link from "next/link";
import { Async, Callout, EmptyState, ErrorNote, Loading, Panel, Section, SegmentedControl, Select } from "@/components/ui";
import { useApi, withQuery } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { useQueryParams } from "@/lib/hooks";
import { rowsOf } from "@/lib/paging";
import type { AssessmentSection, QualityAssessment } from "@/lib/quality";
import type { DrugSummary, StudyListItem } from "@/lib/types";
import { CountLink, ShareBar } from "./parts";

/*
 * Evidence-completeness assessment of one molecule or one trial. Each dimension is a separate panel with the rule
 * of every count and the counts themselves. There is deliberately no summary line, grade or score across panels.
 */

function SectionPanel({ section }: { section: AssessmentSection }) {
  return (
    <Panel as="section" title={section.label} description={section.question} level={3} className="!p-5" >
      <p className="mb-3 text-caption text-ink-3">{section.caveat}</p>
      {section.metrics.length ? (
        <ul className="divide-y divide-hairline">
          {section.metrics.map((m) => (
            <li key={m.id} className="py-2" data-metric={m.id}>
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className="min-w-0 font-medium text-ink">{m.label}</span>
                <span className="num whitespace-nowrap text-data">
                  <CountLink count={m.count} links={m.links} label={m.label.toLowerCase()} /> <span className="text-ink-3">of {formatCount(m.denominator)} {m.denominator_label}</span>
                </span>
              </div>
              <p className="text-caption text-ink-3">{m.rule}</p>
              {m.denominator > 0 && m.id !== "review_rows" && m.id !== "authority_rows" && m.id !== "authority_sources" ? (
                <div className="mt-1 max-w-xs">
                  <ShareBar count={m.count} denominator={m.denominator} />
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-data text-ink-3">Nothing this dimension counts belongs to this selection.</p>
      )}
      {section.by_tier?.length ? (
        <div className="mt-3">
          <h4 className="text-data font-semibold text-ink">Records by evidence tier</h4>
          <ul className="mt-1 space-y-0.5 text-data">
            {section.by_tier.map((t) => (
              <li key={t.value ?? "none"} className="flex justify-between gap-3">
                <span className="text-ink-2">{t.label}</span>
                <span className="num">{t.links ? <Link href={t.links.href}>{formatCount(t.count)}</Link> : formatCount(t.count)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {section.flag_codes?.length ? (
        <div className="mt-3">
          <h4 className="text-data font-semibold text-ink">Check codes on these records</h4>
          <ul className="mt-1 space-y-0.5 text-data">
            {section.flag_codes.map((f) => (
              <li key={f.value} className="flex justify-between gap-3">
                <span className="min-w-0 text-ink-2">
                  {f.title} <span className="text-caption text-ink-3">({f.severity?.toLowerCase() ?? "severity not stated"})</span>
                </span>
                <span className="num">
                  <Link href={f.links.href}>{formatCount(f.count)}</Link>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {section.not_applicable.map((n) => (
        <p key={n} className="mt-2 text-caption text-ink-3">
          {n}
        </p>
      ))}
    </Panel>
  );
}

export function AssessmentPanel() {
  const [param, setParams] = useQueryParams();
  const [kindParam, ...rest] = param("assess").split(":");
  const kind: "drug" | "study" = kindParam === "study" ? "study" : "drug";
  const value = rest.join(":");
  const drugs = useApi<unknown>("/drugs?limit=200");
  const studies = useApi<unknown>("/studies?limit=500");
  const assessment = useApi<QualityAssessment>(value ? withQuery("/quality/assessment", { [kind]: value }) : null);
  const options = kind === "drug" ? rowsOf<DrugSummary>(drugs.data).map((d) => ({ value: d.slug, label: d.name })) : rowsOf<StudyListItem>(studies.data).map((s) => ({ value: s.nct_id, label: `${s.display_name} (${s.nct_id})` }));
  const list = kind === "drug" ? drugs : studies;
  return (
    <Section id="assessment" title="Evidence-completeness assessment" hint="One molecule or one trial, dimension by dimension: each count with the rule that produced it. The panels are not combined and no score is given.">
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <SegmentedControl label="Assess" value={kind} onChange={(k) => setParams({ assess: k === kind ? param("assess") : `${k}:` })} options={[{ value: "drug", label: "A molecule" }, { value: "study", label: "A trial" }]} />
        {list.status === "error" ? (
          <ErrorNote message={`The ${kind === "drug" ? "molecule" : "trial"} list could not be loaded: ${list.error}`} onRetry={list.reload} />
        ) : (
          <Select
            label={kind === "drug" ? "Molecule" : "Trial"}
            value={value}
            onChange={(v) => setParams({ assess: v ? `${kind}:${v}` : null })}
            options={[{ value: "", label: list.status === "success" ? `Choose a ${kind === "drug" ? "molecule" : "trial"}` : "Loading…" }, ...options]}
            className="w-full sm:w-auto"
            disabled={list.status !== "success"}
          />
        )}
      </div>
      {!value ? (
        <EmptyState title={`Choose a ${kind === "drug" ? "molecule" : "trial"} to assess`}>Each quality dimension is then shown separately, with the rule and the counts behind it.</EmptyState>
      ) : (
        <Async state={assessment} errorTitle="The assessment could not be loaded" loading={<Loading rows={6} label="Counting the records of this selection…" />}>
          {(a) => (
            <div data-testid="assessment">
              <Callout tone="info" title={`${a.label}: six separate dimensions`} level={3} className="mb-4">
                <ul className="mt-1 list-disc space-y-0.5 pl-4">
                  {a.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </Callout>
              <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
                {a.sections.map((s) => (
                  <SectionPanel key={s.id} section={s} />
                ))}
              </div>
            </div>
          )}
        </Async>
      )}
    </Section>
  );
}
