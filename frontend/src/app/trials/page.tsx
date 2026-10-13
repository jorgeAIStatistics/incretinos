"use client";

import * as Tabs from "@radix-ui/react-tabs";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Badge, ErrorNote, Loading, PageHeader, Select } from "@/components/ui";
import { phaseLabel, titleCase, useApi, diabetesLabel } from "@/lib/api";

const TIER_SHORT: Record<string, string> = { REGULATORY_LABEL: "Label", REGULATORY_REVIEW: "FDA review", PEER_REVIEWED: "Peer-reviewed", REGISTRY_RESULTS: "Registry", PRESS_RELEASE: "Press release" };

export default function TrialAtlas() {
  const { data: studies, error, loading } = useApi<any[]>("/studies");
  const { data: drugs } = useApi<any[]>("/drugs");
  const [drug, setDrug] = useState("");
  const [evidence, setEvidence] = useState("");
  const [q, setQ] = useState("");
  const { data: index } = useApi(`/discovered${drug ? `?drug=${drug}` : ""}`);
  const rows = useMemo(() => (studies ?? []).filter((s) =>
    (!drug || s.drugs.some((d: any) => d.slug === drug)) &&
    (!q || `${s.display_name} ${s.brief_title} ${s.nct_id}`.toLowerCase().includes(q.toLowerCase())) &&
    (!evidence || (evidence === "none" ? s.evidence_tiers.length === 0 : s.evidence_tiers.includes(evidence)))), [studies, drug, q, evidence]);
  if (error) return <ErrorNote message={error} />;
  return (
    <>
      <PageHeader eyebrow="Clinical trial atlas" title="Every tracked trial, with the tier of evidence behind it" lede="Trials ingested in depth carry protocol, posted results, adverse events and linked publications. The registry index lists every Lilly-led trial matching the tracked agents." />
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Select label="Molecule" value={drug} onChange={setDrug} options={[{ value: "", label: "All molecules" }, ...(drugs ?? []).map((d) => ({ value: d.slug, label: d.name }))]} className="w-56" />
        <Select label="Evidence on record" value={evidence} onChange={setEvidence} options={[{ value: "", label: "Any" }, { value: "PEER_REVIEWED", label: "Peer-reviewed results" }, { value: "REGISTRY_RESULTS", label: "Registry-posted results" }, { value: "PRESS_RELEASE", label: "Press-release results" }, { value: "none", label: "No results on record" }]} className="w-56" />
        <label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">Search
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name, title or NCT number" className="h-9 w-64 rounded-md border border-hairline-strong bg-surface px-2.5 text-[13.5px] font-normal" />
        </label>
      </div>
      <Tabs.Root defaultValue="depth">
        <Tabs.List className="mb-3 flex gap-5 border-b border-hairline" aria-label="Trial sets">
          {[["depth", `Ingested in depth (${rows.length})`], ["index", `Registry index (${index?.total ?? "…"})`]].map(([v, l]) => (
            <Tabs.Trigger key={v} value={v} className="-mb-px border-b-2 border-transparent pb-2 text-[13.5px] font-medium text-ink-3 data-[state=active]:border-accent data-[state=active]:text-ink">{l}</Tabs.Trigger>
          ))}
        </Tabs.List>
        <Tabs.Content value="depth">
          {loading ? <Loading rows={8} /> : (
            <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
              <table className="tbl">
                <thead><tr><th>Trial</th><th>Molecule</th><th>Phase</th><th>Status</th><th>Population</th><th className="r">Enrolled</th><th>Evidence tiers on record</th><th>Primary publication</th><th>Primary completion</th></tr></thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.nct_id}>
                      <td className="min-w-[210px]"><Link href={`/trials/${s.nct_id}`} className="font-semibold">{s.display_name}</Link><span className="mono block text-ink-3">{s.nct_id}</span></td>
                      <td>{s.drugs.filter((d: any) => d.role !== "COMPARATOR").map((d: any) => d.name).join(", ")}{s.drugs.some((d: any) => d.role === "COMPARATOR") ? <span className="block text-[11.5px] text-ink-3">vs {s.drugs.filter((d: any) => d.role === "COMPARATOR").map((d: any) => d.name).join(", ")}</span> : null}</td>
                      <td className="whitespace-nowrap">{phaseLabel(s.phase)}</td>
                      <td>{titleCase(s.overall_status)}</td>
                      <td>{diabetesLabel(s.diabetes_status)}</td>
                      <td className="r num">{s.enrollment?.toLocaleString() ?? "—"}</td>
                      <td><span className="flex flex-wrap gap-1">{s.evidence_tiers.length ? s.evidence_tiers.filter((t: string) => TIER_SHORT[t]).map((t: string) => <Badge key={t} tone={t === "PRESS_RELEASE" ? "warn" : t === "REGISTRY_RESULTS" ? "info" : "ok"} icon={false}>{TIER_SHORT[t]}</Badge>) : <Badge tone="muted">No results on record</Badge>}</span></td>
                      <td>{s.primary_publication ? <span className="text-[12.5px]">{s.primary_publication.journal} {s.primary_publication.year}<span className="mono block text-ink-3">PMID {s.primary_publication.pmid}</span></span> : <span className="text-ink-3">—</span>}</td>
                      <td className="num whitespace-nowrap">{s.primary_completion_date ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Tabs.Content>
        <Tabs.Content value="index">
          <p className="mb-2 text-[12.5px] text-ink-3">{index?.note}</p>
          <div className="max-h-[640px] overflow-auto rounded-lg border border-hairline bg-surface">
            <table className="tbl">
              <thead><tr><th>NCT</th><th>Title</th><th>Phase</th><th>Status</th><th>Results posted</th><th>Primary completion</th><th>Depth</th></tr></thead>
              <tbody>
                {(index?.rows ?? []).filter((t: any) => !q || `${t.brief_title} ${t.acronym ?? ""} ${t.nct_id}`.toLowerCase().includes(q.toLowerCase())).map((t: any) => (
                  <tr key={t.nct_id}>
                    <td className="mono whitespace-nowrap"><a href={`https://clinicaltrials.gov/study/${t.nct_id}`} target="_blank" rel="noopener noreferrer">{t.nct_id}</a></td>
                    <td className="max-w-xl">{t.acronym ? <span className="font-semibold">{t.acronym} — </span> : null}{t.brief_title}</td>
                    <td className="whitespace-nowrap">{phaseLabel(t.phase)}</td><td>{titleCase(t.overall_status)}</td><td>{t.has_results ? "Yes" : "No"}</td><td className="num">{t.primary_completion_date ?? "—"}</td>
                    <td>{t.ingested_in_depth ? <Link href={`/trials/${t.nct_id}`}>In depth</Link> : <span className="text-ink-3">Index only</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Tabs.Content>
      </Tabs.Root>
    </>
  );
}
