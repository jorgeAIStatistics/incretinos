"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Badge, Button, Callout, ErrorNote, KV, Loading, NumberField, PageHeader, Panel, RegulatoryBadge, Section, Select, SourceLine, VerificationBadge } from "@/components/ui";
import { apiGet, fmt, phaseLabel, titleCase, useApi, diabetesLabel } from "@/lib/api";

const TARGET: Record<string, string> = { GLP1R: "GLP-1 receptor", GIPR: "GIP receptor", GCGR: "Glucagon receptor", AMYR: "Amylin receptor", ACTRII: "Activin type II receptors" };

function Harmonizer({ drug }: { drug: any }) {
  const formulations = drug.formulations as any[];
  const [from, setFrom] = useState(formulations[0].key);
  const [amount, setAmount] = useState<number>(36);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const to = formulations.find((f) => f.key !== from)?.key;
  const run = async () => {
    setError(null);
    try {
      setResult(await apiGet(`/harmonize?formulation=${from}&amount=${amount}&target=${to}`));
    } catch (e: any) {
      setResult(null);
      setError(e.message);
    }
  };
  return (
    <Panel>
      <p className="text-[13.5px] font-semibold text-ink">Dose harmonization check</p>
      <p className="mt-0.5 text-[12.5px] text-ink-3">Returns an equivalent dose only when a source documents that exact pair. There is no conversion factor.</p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <Select label="From formulation" value={from} onChange={(v) => { setFrom(v); setResult(null); }} options={formulations.map((f) => ({ value: f.key, label: f.dosage_form ?? f.name }))} className="w-48" />
        <div className="w-28"><NumberField label="Dose" value={amount} onChange={setAmount} step={0.1} min={0.1} suffix="mg" /></div>
        <Button onClick={run} disabled={!Number.isFinite(amount) || amount <= 0}>Look up documented equivalent</Button>
      </div>
      {error ? <div className="mt-3"><ErrorNote message={error} /></div> : null}
      {result ? (
        <div className="mt-3" data-testid="harmonize-result">
          {result.status === "DOCUMENTED" ? (
            <Callout tone="ok" title={`${fmt(amount)} mg corresponds to ${fmt(result.equivalent.amount)} mg — ${result.equivalent.formulation_name}`}>
              Basis: {titleCase(result.basis)}. {result.statement}
              {result.quote ? <span className="mt-1 block text-ink-3">“{result.quote}”</span> : null}
              <span className="mt-1 block"><VerificationBadge status={result.verification_status} /></span>
            </Callout>
          ) : (
            <Callout tone="warn" title="No documented equivalent — no conversion offered">{result.explanation}</Callout>
          )}
        </div>
      ) : null}
    </Panel>
  );
}

function MoleculeDetail({ slug }: { slug: string }) {
  const { data: d, error, loading } = useApi(`/drugs/${slug}`);
  if (error) return <ErrorNote message={error} />;
  if (loading || !d) return <Loading rows={7} />;
  return (
    <div className="fade-in min-w-0">
      <div className="mb-5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <h2 className="text-[22px] font-semibold text-ink">{d.name}</h2>
        {d.code_name ? <span className="mono text-ink-3">{d.code_name}</span> : null}
        {d.brand_names?.length ? <span className="text-[13px] text-ink-2">{d.brand_names.join(" · ")}</span> : null}
        <RegulatoryBadge status={d.regulatory_status} />
      </div>
      <Panel className="mb-6">
        <p className="text-[11.5px] font-medium uppercase tracking-wide text-ink-3">Pharmacologic class — derived from the receptor actions below</p>
        <p className="mt-1 text-[18px] font-semibold text-ink">{d.classification.class_label}</p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Badge tone={d.classification.is_glp1_receptor_agonist ? "info" : "muted"} icon={false}>{d.classification.is_glp1_receptor_agonist ? "GLP-1 receptor agonist activity" : "Not a GLP-1 receptor agonist"}</Badge>
          {d.classification.is_amylin_receptor_agonist ? <Badge tone="info" icon={false}>Amylin receptor agonist activity</Badge> : null}
          {d.classification.is_gip_receptor_agonist ? <Badge tone="info" icon={false}>GIP receptor agonist activity</Badge> : null}
          {d.classification.is_glucagon_receptor_agonist ? <Badge tone="info" icon={false}>Glucagon receptor agonist activity</Badge> : null}
        </div>
        <div className="mt-4 border-t border-hairline pt-3">
          <KV rows={[
            ["Regulatory status", <span key="s">{d.regulatory_summary} <span className="text-ink-3">(as of {d.regulatory_as_of})</span></span>],
            ["Status source", <SourceLine key="src" source={d.regulatory_source} />],
            ["Modality", d.modality], ["Route", d.route], ["Sponsor", d.sponsor],
          ]} />
        </div>
      </Panel>

      <Section title="Mechanism" hint="Each receptor action is a separate, cited statement.">
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl">
            <thead><tr><th>Target</th><th>Action</th><th>Supporting source and quote</th><th>Verification</th></tr></thead>
            <tbody>
              {d.mechanisms.map((m: any, i: number) => (
                <tr key={i}>
                  <td className="font-medium">{TARGET[m.target] ?? m.target}{m.description ? <span className="block text-[12px] font-normal text-ink-3">{m.description}</span> : null}</td>
                  <td>{titleCase(m.action)}</td>
                  <td><SourceLine source={m.source} quote={m.quote} /></td>
                  <td><VerificationBadge status={m.verification_status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {d.indications.length ? (
        <Section title="Indications">
          <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
            <table className="tbl">
              <thead><tr><th>Brand</th><th>Indication</th><th>Status</th><th>Source</th></tr></thead>
              <tbody>
                {d.indications.map((i: any, k: number) => (
                  <tr key={k}><td className="font-medium">{i.brand ?? "—"}</td><td className="max-w-xl">{i.text}</td><td><RegulatoryBadge status={i.status} /></td><td><SourceLine source={i.source} compact /></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      {d.equivalences.length ? (
        <Section title="Formulations and documented dose equivalence" hint={d.equivalence_rule}>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
            <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
              <table className="tbl">
                <thead><tr><th className="r">Investigational capsule</th><th className="r">Marketed tablet</th><th className="r">Tablet ÷ capsule</th><th>Basis</th><th>Verification</th></tr></thead>
                <tbody>
                  {d.equivalences.map((e: any, k: number) => (
                    <tr key={k}><td className="r num font-semibold">{fmt(e.a.amount)} mg</td><td className="r num font-semibold">{fmt(e.b.amount)} mg</td><td className="r num text-ink-3">{fmt(e.b.amount / e.a.amount, 3)}</td><td>{titleCase(e.basis)}</td><td><VerificationBadge status={e.verification_status} /></td></tr>
                  ))}
                </tbody>
              </table>
              <div className="border-t border-hairline px-3 py-2.5 text-[12px] text-ink-3">
                The ratio differs for every pair, so no single factor converts one formulation to the other. <SourceLine source={d.equivalences[0].source} quote={d.equivalences[0].quote} />
              </div>
            </div>
            <Harmonizer drug={d} />
          </div>
        </Section>
      ) : null}

      <Section title={`Tracked trials (${d.studies.length})`}>
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl">
            <thead><tr><th>Trial</th><th>Phase</th><th>Status</th><th>Population</th><th>Registry results</th><th>Primary completion</th></tr></thead>
            <tbody>
              {d.studies.map((s: any) => (
                <tr key={s.nct_id}>
                  <td><Link href={`/trials/${s.nct_id}`} className="font-medium">{s.display_name}</Link> <span className="mono text-ink-3">{s.nct_id}</span></td>
                  <td>{phaseLabel(s.phase)}</td><td>{titleCase(s.overall_status)}</td><td>{diabetesLabel(s.diabetes_status)}</td>
                  <td>{s.has_results ? <Badge tone="ok">Posted</Badge> : <Badge tone="muted">Not posted</Badge>}</td>
                  <td className="num">{s.primary_completion_date ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

function Explorer() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: drugs, error } = useApi<any[]>("/drugs");
  const slug = params.get("drug") ?? "retatrutide";
  if (error) return <ErrorNote message={error} />;
  return (
    <>
      <PageHeader eyebrow="Molecule explorer" title="Mechanism, status and formulation — each with its source" lede="A molecule's class is computed from its recorded receptor actions, and “approved” appears only when FDA labeling has been ingested." />
      <div className="grid gap-6 lg:grid-cols-[230px_minmax(0,1fr)]">
        <nav aria-label="Molecules" className="flex gap-1.5 overflow-x-auto lg:flex-col">
          {(drugs ?? []).map((d) => (
            <button key={d.slug} type="button" onClick={() => router.replace(`/molecules?drug=${d.slug}`)} aria-current={d.slug === slug}
              className={clsx("shrink-0 rounded-md border px-3 py-2 text-left transition-colors", d.slug === slug ? "border-accent bg-accent-soft" : "border-hairline bg-surface hover:bg-sunken")}>
              <span className="block text-[13.5px] font-semibold text-ink">{d.name}</span>
              <span className="block text-[11.5px] text-ink-3">{d.classification.class_label}</span>
            </button>
          ))}
        </nav>
        <MoleculeDetail key={slug} slug={slug} />
      </div>
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Explorer /></Suspense>;
}
