"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { Badge, Callout, ClassificationBadge, ErrorNote, Loading, PageHeader, Panel, RegulatoryBadge, Section, Stat } from "@/components/ui";
import { day, phaseLabel, titleCase, useApi } from "@/lib/api";

export default function CommandCenter() {
  const { data, error, loading } = useApi("/overview");
  const { data: validation } = useApi("/validation");
  if (error) return <ErrorNote message={`The evidence service is not reachable: ${error}`} />;
  if (loading || !data) return <Loading rows={8} />;
  const m = data.meta;
  const verified = (m.result_verification.MACHINE_EXTRACTED ?? 0) + (m.result_verification.QUOTE_ANCHORED ?? 0);
  const pending = m.result_verification.CURATED_PENDING_REVIEW ?? 0;
  return (
    <>
      <PageHeader
        eyebrow="Research command center"
        title="What the public evidence supports — and what it does not"
        lede="Lilly's incretin and amylin portfolio, read from registries, publications, FDA labeling and sponsor disclosures. Every number carries its source, estimand and verification status."
      >
        <Link href="/compare" className="inline-flex h-9 items-center gap-1.5 rounded-md bg-accent px-3.5 text-[13.5px] font-medium text-white hover:bg-[#184f9f] hover:no-underline">
          Open the comparison laboratory <ArrowRight aria-hidden size={15} />
        </Link>
      </PageHeader>

      <div className="mb-8 grid gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
        <Panel className="!p-5">
          <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
            <Stat label="Trials in depth" value={m.counts.studies} note={`${m.counts.discovered_trials} in registry index`} />
            <Stat label="Reported results" value={m.counts.results.toLocaleString()} note={`${m.counts.analyses} between-arm analyses`} />
            <Stat label="Sources" value={m.counts.sources} note="hashed and versioned" />
            <Stat label="Open evidence gaps" value={m.counts.gaps} note={`${m.counts.open_review_items} items awaiting review`} />
          </div>
          <div className="mt-5 border-t border-hairline pt-4">
            <p className="text-[12.5px] font-medium text-ink-2">How the stored results were checked</p>
            <div className="mt-2 flex h-2.5 overflow-hidden rounded-full bg-[var(--grid)]" role="img" aria-label={`${verified} results machine-extracted or quote-anchored; ${pending} pending human review`}>
              <span className="bg-[var(--series-1)]" style={{ width: `${(100 * verified) / (verified + pending || 1)}%` }} />
            </div>
            <p className="mt-2 text-[12.5px] text-ink-3">
              <span className="num font-semibold text-ink">{verified.toLocaleString()}</span> machine-extracted from the registry or anchored verbatim to source text ·{" "}
              <span className="num font-semibold text-ink">{pending.toLocaleString()}</span> curated and awaiting human review · none marked human-verified without a recorded decision.
            </p>
          </div>
        </Panel>
        <Panel className="!p-5">
          <p className="text-[12.5px] font-medium uppercase tracking-wide text-ink-3">Scientific validation rules</p>
          <p className="num mt-1 text-[30px] font-semibold leading-none text-ink">
            {validation ? `${validation.passed} / ${validation.total}` : "—"} <span className="text-[14px] font-medium text-ink-3">passing</span>
          </p>
          <ul className="mt-3 space-y-1.5 text-[12.5px] text-ink-2">
            {(validation?.rules ?? []).slice(0, 5).map((r: any) => (
              <li key={r.rule_id} className="flex items-start gap-2">
                <Badge tone={r.passed ? "ok" : "critical"}>{r.rule_id}</Badge>
                <span className="leading-snug">{r.title}</span>
              </li>
            ))}
          </ul>
          <Link href="/sources?tab=validation" className="mt-3 inline-block text-[12.5px] font-medium">All rules and what they check →</Link>
        </Panel>
      </div>

      <Section title="Portfolio" hint="Pharmacologic class is derived from sourced receptor actions; regulatory status requires FDA labeling to read “approved”.">
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl">
            <thead><tr><th>Molecule</th><th>Class (derived)</th><th>Regulatory status</th><th>Status source</th><th className="r">Trials tracked</th></tr></thead>
            <tbody>
              {data.drugs.map((d: any) => (
                <tr key={d.slug}>
                  <td><Link href={`/molecules?drug=${d.slug}`} className="font-semibold">{d.name}</Link><span className="mono ml-2 text-ink-3">{d.code_name ?? d.brand_names?.join(" / ")}</span>{d.tier_group === "ADJACENT" ? <span className="ml-2 text-[11.5px] text-ink-3">adjacent pipeline</span> : null}</td>
                  <td>{d.classification.class_label}</td>
                  <td><RegulatoryBadge status={d.regulatory_status} /></td>
                  <td className="text-ink-3">{d.regulatory_source?.tier_label} · as of {d.regulatory_as_of}</td>
                  <td className="r num">{d.n_studies}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="grid gap-x-8 lg:grid-cols-2">
        <Section title="Worked comparisons" hint="Each opens with its classification and the reasons for it.">
          <ul className="divide-y divide-hairline rounded-lg border border-hairline bg-surface">
            {data.presets.map((p: any) => (
              <li key={p.title} className="px-4 py-3">
                <Link href={`/compare?a=${p.a}&b=${p.b}`} className="text-[13.5px] font-medium">{p.title}</Link>
                <p className="mt-0.5 text-[12.5px] text-ink-3">{p.question}</p>
                <div className="mt-1.5"><ClassificationBadge value={p.classification} /></div>
              </li>
            ))}
          </ul>
        </Section>
        <div>
          <Section title="Highest-priority open questions" action={<Link href="/gaps" className="text-[12.5px] font-medium">All gaps →</Link>}>
            <ul className="divide-y divide-hairline rounded-lg border border-hairline bg-surface">
              {data.top_gaps.map((g: any) => (
                <li key={g.id} className="px-4 py-3">
                  <p className="text-[13.5px] font-medium leading-snug text-ink">{g.question}</p>
                  <p className="mt-1 text-[12px] text-ink-3">{g.gap_nature_label}</p>
                </li>
              ))}
            </ul>
          </Section>
          <Section title="Source changes awaiting review" action={<Link href="/sources?tab=changes" className="text-[12.5px] font-medium">Review queue →</Link>}>
            {data.changes.length === 0 ? <p className="text-[13px] text-ink-3">No tracked source has changed since the last ingestion.</p> : null}
            {data.changes.map((c: any) => (
              <Callout key={c.id} tone={c.is_synthetic ? "warn" : "info"} title={c.is_synthetic ? "Synthetic demonstration change" : `Detected ${day(c.detected_at)}`}>
                {c.summary} {c.is_synthetic ? "The “before” version was generated from the real record to demonstrate change review; it is not a real registry change." : null}
              </Callout>
            ))}
          </Section>
          <Section title="Next expected primary completions" hint="Registry dates for trials without posted results.">
            <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
              <table className="tbl">
                <thead><tr><th>Trial</th><th>Phase</th><th>Status</th><th>Primary completion</th></tr></thead>
                <tbody>
                  {data.upcoming_readouts.map((s: any) => (
                    <tr key={s.nct_id}>
                      <td><Link href={`/trials/${s.nct_id}`} className="font-medium">{s.display_name}</Link></td>
                      <td>{phaseLabel(s.phase)}</td>
                      <td>{titleCase(s.overall_status)}</td>
                      <td className="num">{s.primary_completion_date} <span className="text-ink-3">({(s.primary_completion_type ?? "").toLowerCase()})</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </div>
      </div>
      <p className="mt-2 border-t border-hairline pt-4 text-[12px] text-ink-3">{m.notice}</p>
    </>
  );
}
