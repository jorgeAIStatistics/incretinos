"use client";

import { Download } from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";
import { ChartFrame, ChartTable, IntervalChart } from "@/components/charts";
import { Badge, Callout, ErrorNote, KV, Loading, PageHeader, Panel, Qualifiers, Section, SourceLine, ValueCell, VerificationBadge } from "@/components/ui";
import { fmt, phaseLabel, titleCase, useApi, diabetesLabel } from "@/lib/api";
import { estimandLabel, findingRow } from "@/lib/rows";

function EndpointGroup({ g, open }: { g: any; open: boolean }) {
  const [expanded, setExpanded] = useState(open);
  return (
    <div className="border-b border-hairline last:border-b-0">
      <button type="button" onClick={() => setExpanded((e) => !e)} aria-expanded={expanded} className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left hover:bg-sunken">
        <span className="min-w-0">
          <span className="block text-[13.5px] font-medium text-ink">{g.title}</span>
          <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
            <Badge tone={g.role === "PRIMARY" ? "info" : g.role === "EXPLORATORY" || g.role === "POST_HOC" ? "warn" : "muted"} icon={false}>{titleCase(g.role)}</Badge>
            <span>{g.origin === "REGISTRY" ? "Registry-posted" : "From publication, label or release"}</span>
            {g.timeframe ? <span>· {g.timeframe}</span> : null}
            <span>· {g.results.length} values · {g.analyses.length} between-arm analyses</span>
          </span>
        </span>
        <span aria-hidden className="mt-0.5 text-ink-3">{expanded ? "−" : "+"}</span>
      </button>
      {expanded ? (
        <div className="overflow-x-auto px-2 pb-3">
          {g.results.length ? (
            <table className="tbl">
              <thead><tr><th>Arm</th><th className="r">Reported value</th><th>Dispersion</th><th className="r">n</th><th>Estimand</th><th>Qualifiers</th><th>Source</th></tr></thead>
              <tbody>
                {g.results.map((f: any) => (
                  <tr key={f.result_id}>
                    <td className="font-medium">{f.arm_label}{f.class_title ? <span className="block text-[11.5px] font-normal text-ink-3">{f.class_title}</span> : null}</td>
                    <td className="r">{f.value == null && f.value_low != null ? <span className="num font-semibold">{fmt(f.value_low)} to {fmt(f.value_high)}</span> : <ValueCell value={f.value} unit={f.unit} lo={f.ci_low} hi={f.ci_high} level={f.ci_level} />}{f.ci_low == null ? <span className="block text-[11.5px] text-ink-3">CI not reported</span> : null}</td>
                    <td className="num text-ink-3">{f.dispersion_value != null ? `${f.dispersion_type} ${fmt(f.dispersion_value)}` : "—"}</td>
                    <td className="r num">{f.n_analyzed ?? <span className="text-ink-3">n/r</span>}</td>
                    <td className="whitespace-nowrap">{estimandLabel(f.estimand_label, f.estimand_basis)}</td>
                    <td className="min-w-[200px]"><Qualifiers labels={f.labels.filter((l: any) => !["ESTIMAND_EFFICACY", "ESTIMAND_TREATMENT_REGIMEN", "CI_NOT_REPORTED", "MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_REVIEW"].includes(l.code))} max={3} /><span className="mt-1 block"><VerificationBadge status={f.verification_status} /></span></td>
                    <td className="min-w-[180px]"><SourceLine source={f.source} compact /> <Link href={`/compare?a=${f.result_id}`} className="block text-[12px]">Compare →</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {g.analyses.length ? (
            <table className="tbl mt-2">
              <thead><tr><th>Contrast</th><th>Estimate</th><th>p-value</th><th>Qualifiers</th><th>Source</th></tr></thead>
              <tbody>
                {g.analyses.map((a: any) => (
                  <tr key={a.id}>
                    <td className="font-medium">{a.treatment_label} <span className="font-normal text-ink-3">vs</span> {a.comparator_label ?? "—"}</td>
                    <td><span className="text-[12px] text-ink-3">{a.param_type}</span><br /><ValueCell value={a.estimate} lo={a.ci_low} hi={a.ci_high} level={a.ci_level} />{a.ci_low == null ? <span className="block text-[11.5px] text-ink-3">CI not reported</span> : null}</td>
                    <td className="num">{a.p_value_text ?? <span className="text-ink-3">not reported</span>}</td>
                    <td className="min-w-[220px]"><Qualifiers labels={a.labels} /></td>
                    <td className="min-w-[180px]"><SourceLine source={a.source} compact quote={a.locator_quote} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function TrialPage({ params }: { params: Promise<{ nct: string }> }) {
  const { nct } = use(params);
  const { data: s, error, loading } = useApi(`/studies/${nct}`);
  if (error) return <ErrorNote message={error} />;
  if (loading || !s) return <Loading rows={9} />;
  const investigational = s.drugs.filter((d: any) => d.role !== "COMPARATOR").map((d: any) => d.slug);
  const weightGroups = s.endpoint_groups.filter((g: any) => g.category === "WEIGHT_PCT_CHANGE" && g.results.some((r: any) => r.scope === "ARM" && r.value != null && !r.is_pooled && !/ minus | to /i.test(r.arm_label)));
  const hero = weightGroups.find((g: any) => g.role === "PRIMARY") ?? weightGroups[0];
  const heroRows = hero ? hero.results.filter((r: any) => r.scope === "ARM" && r.value != null && !r.is_pooled && !/ minus | to /i.test(r.arm_label)).map((f: any) => findingRow(f, { investigational })) : [];
  const heroFirst = hero?.results[0];
  const primaryPub = s.publications.find((p: any) => p.relation === "PRIMARY_RESULTS");
  return (
    <>
      <PageHeader eyebrow={`Clinical trial · ${s.nct_id}`} title={s.display_name} lede={s.brief_title}>
        <a href={`/api/reports/study/${s.nct_id}`} className="inline-flex h-9 items-center gap-1.5 rounded-md border border-hairline-strong bg-surface px-3 text-[13.5px] font-medium text-ink hover:bg-sunken hover:no-underline"><Download aria-hidden size={14} /> Evidence report (.md)</a>
      </PageHeader>
      <div className="mb-7 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
        <Panel>
          <KV rows={[
            ["Phase / status", `${phaseLabel(s.phase)} · ${titleCase(s.overall_status)}`],
            ["Design", `${titleCase(s.allocation)} · ${titleCase(s.masking)} masking · ${titleCase(s.intervention_model)}`],
            ["Enrollment", `${s.enrollment?.toLocaleString() ?? "—"} (${(s.enrollment_type ?? "").toLowerCase()})`],
            ["Population", <span key="p">{diabetesLabel(s.population?.diabetes_status)}<span className="block text-[12px] text-ink-3">{s.population?.diabetes_basis}</span></span>],
            ["Primary completion", `${s.primary_completion_date ?? "—"} (${(s.primary_completion_type ?? "").toLowerCase()})`],
            ["Registry results", s.has_results ? `Posted ${s.results_first_posted ?? ""}` : "Not posted"],
            ["Primary publication", primaryPub ? <span key="pub">{primaryPub.journal} {primaryPub.year} · <a href={`https://pubmed.ncbi.nlm.nih.gov/${primaryPub.pmid}/`} target="_blank" rel="noopener noreferrer">PMID {primaryPub.pmid}</a><span className="block text-[12px] text-ink-3">Linked by: {primaryPub.link_bases.map((b: string) => titleCase(b)).join(", ")} · confidence {primaryPub.link_confidence.toLowerCase()}</span></span> : "None linked"],
            ["Registry record", <SourceLine key="src" source={s.source} compact />],
          ]} />
        </Panel>
        {hero ? (
          <ChartFrame
            title={hero.title}
            subtitle={<>{estimandLabel(heroFirst.estimand_label, heroFirst.estimand_basis)} · {hero.timeframe ?? `week ${fmt(heroFirst.timepoint_weeks, 0)}`} · arm-level values; the treatment effect is the randomized contrast with the control arm.</>}
            table={<ChartTable rows={heroRows} unit="%" valueHeader="Change, %" />}
            footnote={<>Source: <SourceLine source={heroFirst.source} /> · endpoint role: {titleCase(hero.role)}</>}
          >
            <IntervalChart rows={heroRows} unit="%" axisLabel="Percent change in body weight" />
          </ChartFrame>
        ) : (
          <Callout tone="muted" title="No arm-level weight results are on record for this trial">{s.has_results ? "The registry record posts other outcomes; see the endpoint list below." : "The registry has no posted results and no publication or release results have been curated. This is a public reporting gap, not evidence that no data exist."}</Callout>
        )}
      </div>

      {s.estimands.length ? (
        <Section title="Estimands on record" hint="How each analysis handled participants who stopped treatment.">
          <div className="grid gap-3 md:grid-cols-2">
            {s.estimands.map((e: any) => (
              <Panel key={e.id}>
                <div className="flex flex-wrap items-center gap-2"><span className="text-[14px] font-semibold">{estimandLabel(e.label)}</span><Badge tone={e.basis === "STATED_BY_SOURCE" ? "ok" : e.basis.includes("INFERRED") ? "warn" : "muted"}>{titleCase(e.basis)}</Badge></div>
                <p className="mt-1.5 text-[13px] text-ink-2">{e.explainer}</p>
                {e.quote ? <p className="mt-1.5 text-[12.5px] text-ink-3">Basis in source: “…{e.quote}…”</p> : null}
                <p className="mt-1.5"><SourceLine source={e.source} compact /></p>
              </Panel>
            ))}
          </div>
        </Section>
      ) : null}

      <Section title="Reported results" hint="Grouped by endpoint. Registry-posted and publication-reported values are kept as separate rows even when they describe the same arm.">
        <div className="rounded-lg border border-hairline bg-surface">
          {s.endpoint_groups.length ? s.endpoint_groups.slice(0, 60).map((g: any, i: number) => <EndpointGroup key={g.endpoint_id} g={g} open={i === 0} />) : <p className="px-4 py-3 text-[13px] text-ink-3">No results stored.</p>}
        </div>
      </Section>

      <div className="grid gap-x-8 lg:grid-cols-2">
        <Section title="Arms as registered">
          <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
            <table className="tbl"><thead><tr><th>Arm</th><th>Type</th><th className="r">Started</th></tr></thead>
              <tbody>{s.arms.filter((a: any) => a.origin === "REGISTRY" && a.arm_type !== "RESULTS_GROUP").map((a: any) => (<tr key={a.id}><td className="font-medium">{a.label}{a.formulation_key ? <span className="block text-[11.5px] font-normal text-ink-3">Formulation: {a.formulation_key}</span> : null}</td><td>{titleCase(a.arm_type)}</td><td className="r num">{a.n_randomized ?? "—"}</td></tr>))}</tbody>
            </table>
          </div>
        </Section>
        <Section title={`Linked publications (${s.publications.length})`} hint="Each link lists the independent identifiers that support it.">
          <ul className="max-h-[360px] divide-y divide-hairline overflow-auto rounded-lg border border-hairline bg-surface">
            {s.publications.slice(0, 40).map((p: any) => (
              <li key={p.pmid} className="px-4 py-2.5 text-[13px]">
                {p.relation === "PRIMARY_RESULTS" ? <Badge tone="info" icon={false}>Primary results</Badge> : null} <a href={`https://pubmed.ncbi.nlm.nih.gov/${p.pmid}/`} target="_blank" rel="noopener noreferrer" className="font-medium">{p.title}</a>
                <span className="block text-[12px] text-ink-3">{p.journal} {p.year} · PMID {p.pmid} · {p.link_bases.map((b: string) => titleCase(b)).join(" + ")}</span>
              </li>
            ))}
            {s.publications.length === 0 ? <li className="px-4 py-3 text-[13px] text-ink-3">No publication is linked to this registration in the registry or in PubMed.</li> : null}
          </ul>
        </Section>
      </div>
      <Section title="Registered protocol endpoints">
        <div className="max-h-[320px] overflow-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl"><thead><tr><th>Role</th><th>Outcome</th><th>Time frame</th></tr></thead>
            <tbody>{s.protocol_endpoints.map((e: any, i: number) => (<tr key={i}><td>{titleCase(e.role)}</td><td>{e.title}</td><td className="text-ink-3">{e.timeframe}</td></tr>))}</tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
