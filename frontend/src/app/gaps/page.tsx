"use client";

import { Fragment, useMemo, useState } from "react";
import { MicroBar } from "@/components/charts";
import { Badge, ErrorNote, Loading, PageHeader, Panel, Select } from "@/components/ui";
import { titleCase, useApi } from "@/lib/api";

const NATURE_TONE: Record<string, any> = { PUBLIC_REPORTING_GAP: "warn", EVIDENCE_IN_GENERATION: "info", NOT_KNOWN_TO_BE_STUDIED: "muted", METHODOLOGICAL: "muted", SOURCE_CONFLICT: "critical", RECORD_CHANGE: "warn" };
const NATURE_SHORT: Record<string, string> = { PUBLIC_REPORTING_GAP: "Public reporting gap", EVIDENCE_IN_GENERATION: "Evidence in generation", NOT_KNOWN_TO_BE_STUDIED: "No tracked study found", METHODOLOGICAL: "Methodological", SOURCE_CONFLICT: "Source conflict", RECORD_CHANGE: "Record change" };

function Slider({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">
      <span className="flex justify-between"><span>{label}</span><span className="num text-ink-3">{value.toFixed(2)}</span></span>
      <input type="range" min={0} max={1} step={0.05} value={value} onChange={(e) => onChange(Number(e.target.value))} className="accent-[var(--accent)]" />
    </label>
  );
}

export default function Gaps() {
  const [w, setW] = useState({ importance: 0.4, uncertainty: 0.3, tractability: 0.15, actionability: 0.15 });
  const [drug, setDrug] = useState("");
  const [nature, setNature] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const query = useMemo(() => `/gaps?w_importance=${w.importance}&w_uncertainty=${w.uncertainty}&w_tractability=${w.tractability}&w_actionability=${w.actionability}${drug ? `&drug=${drug}` : ""}${nature ? `&nature=${nature}` : ""}`, [w, drug, nature]);
  const { data, error } = useApi(query);
  const { data: drugs } = useApi<any[]>("/drugs");
  if (error) return <ErrorNote message={error} />;
  return (
    <>
      <PageHeader eyebrow="Evidence gap tracker" title="What the public record does not yet answer — and why" lede="Each gap states what is available, what is missing, and the reason it is open. A gap in public reporting is not confirmation that the sponsor has no data." />
      <div className="mb-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex flex-wrap items-end gap-3">
          <Select label="Molecule" value={drug} onChange={setDrug} options={[{ value: "", label: "All molecules" }, ...(drugs ?? []).filter((d) => d.tier_group !== "COMPARATOR").map((d) => ({ value: d.slug, label: d.name }))]} className="w-60" />
          <Select label="Why the question is open" value={nature} onChange={setNature} options={[{ value: "", label: "Any reason" }, ...Object.entries(NATURE_SHORT).map(([v, l]) => ({ value: v, label: l }))]} className="w-60" />
          <p className="pb-2 text-[12.5px] text-ink-3">{data ? `${data.total} open questions` : ""}</p>
        </div>
        <Panel>
          <p className="text-[12.5px] font-semibold text-ink">Prioritization weights</p>
          <p className="mb-2 text-[12px] text-ink-3">Priority = Σ weight × component (weights normalised to 1). Change them to match your programme's needs.</p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2">
            <Slider label="Clinical importance" value={w.importance} onChange={(v) => setW({ ...w, importance: v })} />
            <Slider label="Uncertainty" value={w.uncertainty} onChange={(v) => setW({ ...w, uncertainty: v })} />
            <Slider label="Tractability" value={w.tractability} onChange={(v) => setW({ ...w, tractability: v })} />
            <Slider label="Actionable now" value={w.actionability} onChange={(v) => setW({ ...w, actionability: v })} />
          </div>
        </Panel>
      </div>
      {!data ? <Loading rows={8} /> : (
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl">
            <thead><tr><th className="r">#</th><th>Open question</th><th>Molecule</th><th>Why it is open</th><th>Certainty</th><th>Priority</th></tr></thead>
            <tbody>
              {data.rows.map((g: any, i: number) => (
                <Fragment key={g.id}>
                  <tr className="cursor-pointer" onClick={() => setOpen(open === g.id ? null : g.id)}>
                    <td className="r num text-ink-3">{i + 1}</td>
                    <td className="max-w-xl"><button type="button" aria-expanded={open === g.id} className="text-left font-medium text-ink hover:text-accent">{g.question}</button><span className="block text-[11.5px] text-ink-3">{titleCase(g.category)}{g.review_status !== "OPEN" ? ` · ${titleCase(g.review_status)}` : ""}</span></td>
                    <td className="whitespace-nowrap">{g.drug?.name ?? "—"}</td>
                    <td><Badge tone={NATURE_TONE[g.gap_nature]} title={g.gap_nature_label}>{NATURE_SHORT[g.gap_nature] ?? titleCase(g.gap_nature)}</Badge></td>
                    <td>{titleCase(g.evidence_certainty)}</td>
                    <td><MicroBar value={g.priority.score} label={Object.entries(g.priority.components).map(([k, v]) => `${k}: ${(v as number).toFixed(2)}`).join(" · ")} /></td>
                  </tr>
                  {open === g.id ? (
                    <tr className="!bg-sunken">
                      <td />
                      <td colSpan={5} className="pb-4">
                        <div className="grid gap-x-8 gap-y-3 md:grid-cols-2 text-[13px]">
                          <div><p className="font-semibold text-ink">Evidence already available</p><p className="text-ink-2">{g.available_evidence}</p></div>
                          <div><p className="font-semibold text-ink">Missing evidence</p><p className="text-ink-2">{g.missing_evidence}</p></div>
                          <div><p className="font-semibold text-ink">Potential analytical approach</p><p className="text-ink-2">{g.analytic_approach}</p></div>
                          <div><p className="font-semibold text-ink">New data required</p><p className="text-ink-2">{g.data_required}</p></div>
                        </div>
                        <p className="mt-3 text-[12.5px] text-ink-3">{g.gap_nature_label}</p>
                        <p className="num mt-1 text-[12.5px] text-ink-3">Priority {g.priority.score.toFixed(3)} = {Object.entries(g.priority.components).map(([k, v]) => `${(g.priority.weights[k] as number).toFixed(2)} × ${(v as number).toFixed(2)} (${k.replace("_", " ")})`).join(" + ")}</p>
                        <p className="mt-1 text-[12.5px]">{g.related_ncts.map((n: string) => <a key={n} href={`/trials/${n}`} className="mr-3 mono">{n}</a>)}{g.source_ids.slice(0, 4).map((id: number) => <a key={id} href={`/sources?id=${id}`} className="mr-3">source #{id}</a>)}<span className="text-ink-3">rule {g.rule_id}</span></p>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-[12px] text-ink-3">{data?.note}</p>
    </>
  );
}
