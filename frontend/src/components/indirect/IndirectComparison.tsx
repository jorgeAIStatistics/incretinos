"use client";

import clsx from "clsx";
import { Ban, Check, HelpCircle } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Callout, EpistemicBadge, ErrorNote, Loading, Panel, Select, SourceLine, Term, VerificationBadge } from "@/components/ui";
import { apiPost, diabetesLabel, fmt, titleCase, useApi } from "@/lib/api";
import { differenceUnit } from "@/lib/rows";
import { NetworkDiagram } from "./NetworkDiagram";
import type { Assessment, Candidate, Check as CheckRow, EffectModifier, IndirectInput, Options } from "./types";

const STATUS: Record<string, { tone: string; Icon: typeof Check; label: string }> = {
  PASS: { tone: "text-[var(--ok)]", Icon: Check, label: "Pass" },
  FAIL: { tone: "text-[var(--critical)]", Icon: Ban, label: "Fail" },
  CANNOT_ASSESS: { tone: "text-[var(--warn)]", Icon: HelpCircle, label: "Cannot assess" },
};

const MODIFIER: Record<string, string> = {
  SIMILAR: "Similar (trial means)",
  DIFFERS: "Differs",
  MAJOR_DIFFERENCE: "Major difference",
  NOT_STORED: "Not stored",
  DESCRIBED: "Described only",
};

const CATEGORY_DEFAULT = "WEIGHT_PCT_CHANGE";

function StatusMark({ status }: { status: string }) {
  const s = STATUS[status] ?? STATUS.CANNOT_ASSESS;
  return <span className={clsx("inline-flex items-center gap-1 whitespace-nowrap text-[12.5px] font-medium", s.tone)}><s.Icon aria-hidden size={13} strokeWidth={2.4} /> {s.label}</span>;
}

function candidateLabel(c: Candidate): string {
  const ci = c.ci_low != null && c.ci_high != null ? ` (${c.ci_level != null ? `${fmt(c.ci_level)}% ` : ""}CI ${fmt(c.ci_low)} to ${fmt(c.ci_high)})` : " (no CI)";
  const flags = c.usable_alone ? "" : " · cannot enter a calculation";
  return `${c.study_name} · ${c.treatment_label} vs ${c.comparator_label} · ${c.estimate != null ? fmt(c.estimate) : "no estimate"}${ci} · week ${c.timepoint_weeks != null ? fmt(c.timepoint_weeks) : "?"} · ${titleCase(c.estimand_label)} estimand · ${c.verification_status}${flags}`;
}

function CandidatePicker({ side, drug, comparator, category, value, onChange }: { side: "A" | "B"; drug: string; comparator: string; category: string; value: string; onChange: (v: string) => void }) {
  const path = drug && comparator ? `/indirect/candidates?drug=${encodeURIComponent(drug)}&comparator=${encodeURIComponent(comparator)}&category=${category}` : null;
  const list = useApi<{ rows: Candidate[] }>(path);
  const rows = list.data?.rows ?? [];
  const chosen = rows.find((r) => String(r.analysis_id) === value);
  const placeholder = !path ? "Choose the drug and the comparator first" : list.status === "error" ? "Analyses could not be loaded" : !list.data ? "Loading analyses…" : rows.length ? "Select an analysis…" : "No stored analysis of this drug against this comparator";
  return (
    <div className="min-w-0">
      <Select label={`Analysis ${side}: ${side === "A" ? "drug A" : "drug B"} versus the comparator`} value={value} onChange={onChange} options={[{ value: "", label: placeholder }, ...rows.map((r) => ({ value: String(r.analysis_id), label: candidateLabel(r) }))]} />
      {list.status === "error" ? <div className="mt-2"><ErrorNote message={`The analyses could not be loaded. ${list.error ?? ""}`} onRetry={list.reload} /></div> : null}
      {chosen && !chosen.usable_alone ? (
        <p className="mt-1.5 text-[12.5px] text-ink-3" data-testid={`indirect-candidate-${side.toLowerCase()}-problems`}>
          On its own this analysis cannot enter a calculation: {chosen.problems.join("; ")}.
        </p>
      ) : null}
    </div>
  );
}

function Checklist({ checks }: { checks: CheckRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-hairline bg-surface" data-testid="indirect-checklist">
      <table className="tbl">
        <caption className="sr-only">Feasibility requirements for an anchored indirect comparison, with the stored evidence for each analysis</caption>
        <thead><tr><th>Requirement</th><th>Status</th><th>Analysis A</th><th>Analysis B</th><th>Why</th></tr></thead>
        <tbody>
          {checks.map((c) => (
            <tr key={c.key} data-testid={`indirect-check-${c.key}`} data-status={c.status}>
              <td className="font-medium">{c.key === "transitivity" ? <Term k="transitivity">Transitivity</Term> : c.key === "effect_modifiers" ? <><Term k="effect-modifier">Effect modifiers</Term> (plausible)</> : c.label}</td>
              <td><StatusMark status={c.status} /></td>
              <td className="max-w-[220px] break-words text-ink-2">{c.a}</td>
              <td className="max-w-[220px] break-words text-ink-2">{c.b}</td>
              <td className="min-w-[280px] text-ink-2">{c.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Modifiers({ modifiers }: { modifiers: EffectModifier[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-hairline bg-surface" data-testid="indirect-modifiers">
      <table className="tbl">
        <caption className="sr-only">Plausible effect modifiers with the values stored for each trial</caption>
        <thead><tr><th>Effect modifier</th><th>Trial of analysis A</th><th>Trial of analysis B</th><th>Assessment</th><th>Note</th></tr></thead>
        <tbody>
          {modifiers.map((m) => (
            <tr key={m.key}>
              <td className="font-medium">{m.label}</td>
              <td className="max-w-[240px] break-words text-ink-2">{m.a}</td>
              <td className="max-w-[240px] break-words text-ink-2">{m.b}</td>
              <td className="whitespace-nowrap text-ink-2">{MODIFIER[m.assessment] ?? titleCase(m.assessment)}</td>
              <td className="min-w-[240px] text-ink-2">{m.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Inputs({ inputs }: { inputs: IndirectInput[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-hairline bg-surface" data-testid="indirect-inputs">
      <table className="tbl">
        <caption className="sr-only">The two stored contrasts used, with their sources and verification statuses</caption>
        <thead><tr><th>Input</th><th>Contrast as reported</th><th>Interval and SE</th><th>Estimand · population</th><th>Source and status</th></tr></thead>
        <tbody>
          {inputs.map((i) => (
            <tr key={i.side}>
              <td className="font-medium">{i.side === "A" ? "A vs C" : "B vs C"}<br /><span className="font-normal text-ink-3">{i.study_name}</span></td>
              <td className="text-ink-2">{i.orientation}: <span className="num font-semibold text-ink">{i.estimate_text ?? fmt(i.estimate)}</span>{i.scale === "DIFFERENCE" ? ` ${differenceUnit(i.unit)}` : ""}<br />{i.param_type} · week {fmt(i.timepoint_weeks)} · p {i.p_value_text ?? "not reported"}</td>
              <td className="num text-ink-2">{i.ci_level != null ? `${fmt(i.ci_level)}% CI` : "CI"} {fmt(i.ci_low)} to {fmt(i.ci_high)} ({titleCase(i.ci_sides)})<br />{i.se != null ? <>SE {fmt(i.se, 4)} <span className="text-ink-3">({i.se_formula})</span></> : "SE not derivable"}{i.se_rounding_note ? <><br /><span className="text-ink-3">{i.se_rounding_note}</span></> : null}</td>
              <td className="max-w-[300px] text-ink-2">{titleCase(i.estimand_label)} ({i.estimand_basis.toLowerCase().replace(/_/g, " ")}) · {diabetesLabel(i.diabetes_status)}<br /><span className="text-ink-3">{i.analysis_population?.[0] ?? "Analysis population not stored"} (n {i.n_treatment ?? "?"} vs {i.n_comparator ?? "?"})</span></td>
              <td className="max-w-[260px] [overflow-wrap:anywhere]"><VerificationBadge status={i.verification_status} compact /> <SourceLine source={i.source} compact location={i.origin === "REGISTRY" ? i.source_location : null} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Outcome({ out }: { out: Assessment }) {
  const r = out.result;
  return (
    <div className="mt-4 space-y-4" data-testid="indirect-outcome" data-status={out.status}>
      {r ? (
        <div className="rounded-lg border-2 border-dashed border-accent bg-surface p-4" data-testid="indirect-result">
          <div className="flex flex-wrap items-center gap-2">
            <EpistemicBadge kind="INFERENCE">Calculated here — anchored indirect estimate</EpistemicBadge>
            <span className="text-data font-medium text-ink-3">Not a randomized comparison · not a ranking</span>
          </div>
          <p className="mt-2 text-data font-medium text-ink-3">{out.label}</p>
          <p className="mt-0.5 text-[14px] text-ink">{r.orientation}</p>
          <p className="num mt-1 text-[26px] font-semibold leading-tight text-ink" data-testid="indirect-estimate">
            {fmt(r.estimate, 2)}{r.scale === "DIFFERENCE" ? <span className="ml-1.5 text-[13px] font-normal text-ink-3">{differenceUnit(r.unit) || "unit not recorded"}</span> : <span className="ml-1.5 text-[13px] font-normal text-ink-3">ratio</span>}
            <span className="ml-3 text-[14px] font-normal text-ink-2" data-testid="indirect-ci">{fmt(r.ci_level * 100)}% CI {fmt(r.ci_low, 2)} to {fmt(r.ci_high, 2)} (two-sided)</span>
          </p>
          <p className="num mt-1 text-[12.5px] text-ink-2">SE {fmt(r.se, 4)} · z = {fmt(r.z_statistic, 2)} · two-sided p {r.p_value_text}</p>
          <p className="mt-2 text-[13px] text-ink-2" data-testid="indirect-reading">{r.reading}</p>
          <p className="num mt-2 rounded-control bg-sunken px-3 py-2 text-[12.5px] text-ink-2" data-testid="indirect-formula">{r.formula}</p>
          <div className="mt-3">
            <Callout tone="warn" title="Read this before using the number">
              <p>{out.not_a_randomized_comparison}</p>
              <p className="mt-1">{out.not_a_ranking}</p>
              {out.multiplicity_note ? <p className="mt-1">{out.multiplicity_note}</p> : null}
            </Callout>
          </div>
          {out.unverified_assumptions?.length ? (
            <div className="mt-3" data-testid="indirect-unverified">
              <Callout tone="warn" title="Unverified assumptions (could not be assessed from the stored data)">
                <ul className="mt-1 list-disc space-y-1 pl-5">{out.unverified_assumptions.map((x) => <li key={x.key}><span className="font-medium text-ink">{x.label}:</span> {x.detail}</li>)}</ul>
              </Callout>
            </div>
          ) : null}
          <div className="mt-3">
            <p className="text-[13px] font-semibold text-ink">Assumptions of the calculation</p>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-[13px] text-ink-2">{r.assumptions.map((s) => <li key={s}>{s}</li>)}</ul>
          </div>
        </div>
      ) : (
        <div data-testid="indirect-refusal">
          <Callout tone="critical" title="Refused: no anchored indirect estimate is calculated" level={3}>
            <p>{out.headline}</p>
            <ul className="mt-1.5 list-disc space-y-1 pl-5" data-testid="indirect-refusal-reasons">{out.refusal_reasons.map((s) => <li key={s}>{s}</li>)}</ul>
          </Callout>
        </div>
      )}
      <Panel title="Network of the two contrasts" level={3}>
        <NetworkDiagram network={out.network} />
      </Panel>
      <div>
        <h3 className="mb-2 text-[13.5px] font-semibold text-ink">Feasibility checklist</h3>
        <Checklist checks={out.checks} />
      </div>
      <div>
        <h3 className="mb-2 text-[13.5px] font-semibold text-ink">Inputs, with their sources</h3>
        <Inputs inputs={out.inputs} />
      </div>
      <div>
        <h3 className="mb-2 text-[13.5px] font-semibold text-ink">Plausible <Term k="effect-modifier">effect modifiers</Term></h3>
        <Modifiers modifiers={out.effect_modifiers} />
      </div>
      {(() => {
        const t = out.checks.find((c) => c.key === "transitivity");
        if (!t) return null;
        const e = t.evidence ?? {};
        return (
          <Panel title="Transitivity assessment" level={3}>
            <p className="text-[13px] text-ink-2">{t.detail}</p>
            <div className="mt-2 grid grid-cols-1 gap-3 text-[13px] text-ink-2 md:grid-cols-3">
              <div><p className="font-medium text-ink">Supported by</p><ul className="mt-1 list-disc space-y-0.5 pl-5">{(e.supported_by ?? []).map((s) => <li key={s}>{s}</li>)}{!(e.supported_by ?? []).length ? <li>Nothing in the stored data.</li> : null}</ul></div>
              <div><p className="font-medium text-ink">Undermined by</p><ul className="mt-1 list-disc space-y-0.5 pl-5">{(e.undermined_by ?? []).map((s) => <li key={s}>{s}</li>)}{!(e.undermined_by ?? []).length ? <li>No stored difference.</li> : null}</ul></div>
              <div><p className="font-medium text-ink">Cannot be checked</p><ul className="mt-1 list-disc space-y-0.5 pl-5">{(e.unverifiable ?? []).map((s) => <li key={s}>{s}</li>)}</ul></div>
            </div>
          </Panel>
        );
      })()}
      <p className="text-[12px] text-ink-3">Method: {out.method}. {out.reference}</p>
    </div>
  );
}

/**
 * Anchored indirect comparison (Bucher). Kept apart from the direct randomized comparison above: its own section,
 * a dashed outline on the result, an "Inference" badge, and the words "not a randomized comparison" wherever the
 * number appears. Only analyses the engine lists can be chosen; the engine decides feasibility, never this page.
 */
export function IndirectComparison() {
  const [category, setCategory] = useState(CATEGORY_DEFAULT);
  const [drugA, setDrugA] = useState("");
  const [drugB, setDrugB] = useState("");
  const [comparator, setComparator] = useState("");
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [out, setOut] = useState<Assessment | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0); // a slower, older answer must not replace a newer pair
  const options = useApi<Options>(`/indirect/options?category=${category}`);
  const drugs = useMemo(() => options.data?.drugs ?? [], [options.data]);
  const comparators = useMemo(() => {
    const keys = (slug: string) => new Set((drugs.find((d) => d.slug === slug)?.comparators ?? []).map((c) => c.key));
    const shared = drugA && drugB ? [...keys(drugA)].filter((k) => keys(drugB).has(k)) : [];
    return (options.data?.comparators ?? []).filter((c) => shared.includes(c.key));
  }, [drugs, drugA, drugB, options.data]);

  /** Any change upstream clears the analyses and the answer: a pair is assessed only as chosen. */
  const restart = () => { request.current += 1; setA(""); setB(""); setOut(null); setError(null); setBusy(false); };
  const assess = (nextA: string, nextB: string) => {
    setA(nextA); setB(nextB);
    const id = ++request.current;
    if (!nextA || !nextB) { setOut(null); setBusy(false); return; }
    setBusy(true); setError(null);
    apiPost<Assessment>("/indirect/assess", { a: Number(nextA), b: Number(nextB) })
      .then((r) => { if (request.current === id) setOut(r); })
      .catch((e: unknown) => { if (request.current === id) { setError(e instanceof Error ? e.message : String(e)); setOut(null); } })
      .finally(() => { if (request.current === id) setBusy(false); });
  };
  const pickDrug = (side: "A" | "B", slug: string) => {
    if (side === "A") setDrugA(slug); else setDrugB(slug);
    setComparator("");
    restart();
  };

  const drugOptions = (exclude: string) => [{ value: "", label: options.data ? "Select a drug…" : "Loading…" }, ...drugs.filter((d) => d.slug !== exclude).map((d) => ({ value: d.slug, label: `${d.name} (${d.analyses} stored analyses)` }))];
  return (
    <section id="indirect" aria-labelledby="indirect-heading" className="mt-10 border-t-2 border-dashed border-hairline pt-6" data-testid="indirect-section">
      <p className="text-data font-medium text-ink-3">Separate from the direct comparison above</p>
      <h2 id="indirect-heading" className="mt-0.5 text-[20px] font-semibold text-ink">Anchored indirect comparison</h2>
      <p className="mt-1 max-w-[860px] text-[13.5px] text-ink-2">
        An <Term k="anchored-indirect-comparison">anchored indirect comparison</Term> estimates drug A versus drug B from two trials that share a comparator C. It is calculated only when
        every requirement below passes, and it rests on <Term k="transitivity">transitivity</Term>: the two trials must not differ in any <Term k="effect-modifier">effect modifier</Term>.
        It is never a randomized result.
      </p>
      {options.status === "error" ? <div className="mt-3"><ErrorNote message={`The drugs and comparators could not be loaded. ${options.error ?? ""}`} onRetry={options.reload} /></div> : null}
      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-4">
        <Select label="Endpoint for the indirect comparison" value={category} onChange={(v) => { setCategory(v); setDrugA(""); setDrugB(""); setComparator(""); restart(); }} options={(options.data?.categories ?? [{ key: category, label: titleCase(category), analyses: 0 }]).map((c) => ({ value: c.key, label: `${c.label.charAt(0).toUpperCase()}${c.label.slice(1)}` }))} />
        <Select label="Drug A" value={drugA} onChange={(v) => pickDrug("A", v)} options={drugOptions(drugB)} />
        <Select label="Drug B" value={drugB} onChange={(v) => pickDrug("B", v)} options={drugOptions(drugA)} />
        <Select label="Common comparator C" value={comparator} onChange={(v) => { setComparator(v); restart(); }} options={[{ value: "", label: drugA && drugB ? (comparators.length ? "Select the comparator…" : "No comparator shared by both drugs") : "Choose drugs A and B first" }, ...comparators.map((c) => ({ value: c.key, label: c.label }))]} />
      </div>
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        <CandidatePicker key={`A-${drugA}-${comparator}-${category}`} side="A" drug={drugA} comparator={comparator} category={category} value={a} onChange={(v) => assess(v, b)} />
        <CandidatePicker key={`B-${drugB}-${comparator}-${category}`} side="B" drug={drugB} comparator={comparator} category={category} value={b} onChange={(v) => assess(a, v)} />
      </div>
      {error ? <div className="mt-4"><ErrorNote message={error} /></div> : null}
      {busy ? <div className="mt-4"><Loading rows={3} label="Assessing feasibility…" /></div> : null}
      {out && !busy ? <Outcome out={out} /> : null}
    </section>
  );
}
