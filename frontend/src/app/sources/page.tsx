"use client";

import * as Tabs from "@radix-ui/react-tabs";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Badge, Button, Callout, ErrorNote, KV, Loading, PageHeader, Panel, Select, VerificationBadge } from "@/components/ui";
import { apiPost, day, fmt, titleCase, useApi } from "@/lib/api";

const TYPE: Record<string, string> = { CTGOV: "ClinicalTrials.gov", PUBMED: "PubMed", OPENFDA_LABEL: "FDA labeling", OPENFDA_FAERS: "FAERS (openFDA)", OPENFDA_LABEL_SEARCH: "FDA label database check", WEB: "Web (release, review, pipeline)", GUIDANCE: "Guidance" };

function SourceDetail({ id }: { id: number }) {
  const { data: s, error } = useApi(`/sources/${id}`);
  if (error) return <ErrorNote message={error} />;
  if (!s) return <Loading rows={4} />;
  return (
    <Panel className="fade-in" >
      <p className="text-[11.5px] font-semibold uppercase tracking-wide text-ink-3">{TYPE[s.source_type] ?? s.source_type} · {s.tier_label}</p>
      <p className="mt-0.5 text-[15px] font-semibold leading-snug text-ink">{s.title}</p>
      <div className="mt-3"><KV rows={[
        ["Source key", <span key="k" className="mono">{s.key}</span>], ["Publisher", s.publisher], ["Published / updated", s.published_date], ["Retrieved", day(s.retrieved_at)], ["Version", s.version_label],
        ["Content hash (SHA-256)", <span key="h" className="mono break-all">{s.content_hash}</span>],
        ["Original", s.url ? <a key="u" href={s.url} target="_blank" rel="noopener noreferrer" className="break-all">{s.url}</a> : "—"],
        ["Storage and licence", s.license_note],
      ]} /></div>
      <p className="mt-4 text-[12.5px] font-semibold text-ink">Snapshots ({s.snapshots.length})</p>
      <table className="tbl mt-1"><thead><tr><th>Version</th><th>Retrieved</th><th>Hash</th><th>Text stored</th><th>Quotes anchored</th></tr></thead>
        <tbody>{s.snapshots.map((v: any) => <tr key={v.version_no}><td className="num">{v.version_no}{v.is_synthetic ? <span className="ml-1.5"><Badge tone="warn" icon={false}>synthetic</Badge></span> : null}</td><td className="num">{day(v.retrieved_at)}</td><td className="mono">{v.content_hash.slice(0, 16)}…</td><td>{v.content_stored ? "Yes (public-domain / CC0)" : "No — hash and excerpts only"}</td><td className="num">{v.anchored_excerpts.length}</td></tr>)}</tbody></table>
      {s.snapshots.at(-1)?.anchored_excerpts.length ? <details className="mt-2 text-[12.5px]"><summary className="cursor-pointer font-medium text-accent">Anchored locator quotes</summary><ul className="mt-1 list-disc space-y-1 pl-5 text-ink-2">{s.snapshots.at(-1).anchored_excerpts.slice(0, 30).map((q: string) => <li key={q}>“{q}”</li>)}</ul></details> : null}
      {s.dependent_findings.length ? (
        <>
          <p className="mt-4 text-[12.5px] font-semibold text-ink">Findings that depend on this source ({s.dependent_findings.length}{s.dependent_findings.length === 60 ? "+" : ""})</p>
          <div className="max-h-[260px] overflow-auto"><table className="tbl mt-1"><thead><tr><th>Study</th><th>Arm</th><th>Endpoint</th><th className="r">Value</th><th>Verification</th></tr></thead>
            <tbody>{s.dependent_findings.map((f: any) => <tr key={f.result_id}><td>{f.study}</td><td>{f.arm}</td><td className="max-w-[240px] truncate">{f.endpoint}</td><td className="r num"><a href={`/compare?a=${f.result_id}`}>{fmt(f.value)}</a></td><td><VerificationBadge status={f.verification_status} /></td></tr>)}</tbody></table></div>
        </>
      ) : null}
    </Panel>
  );
}

function SourcesTab() {
  const params = useSearchParams();
  const router = useRouter();
  const selected = Number(params.get("id")) || null;
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const { data } = useApi(`/sources?limit=600${type ? `&source_type=${type}` : ""}${q.length > 1 ? `&q=${encodeURIComponent(q)}` : ""}`);
  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
      <div>
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <Select label="Source type" value={type} onChange={setType} options={[{ value: "", label: "All types" }, ...Object.entries(TYPE).map(([v, l]) => ({ value: v, label: `${l}${data?.by_type?.[v] ? ` (${data.by_type[v]})` : ""}` }))]} className="w-64" />
          <label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">Search<input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Title, NCT, PMID…" className="h-9 w-56 rounded-md border border-hairline-strong bg-surface px-2.5 text-[13.5px] font-normal" /></label>
        </div>
        <div className="max-h-[720px] overflow-auto rounded-lg border border-hairline bg-surface">
          <table className="tbl"><thead><tr><th>Source</th><th>Tier</th><th>Retrieved</th><th className="r">Findings</th></tr></thead>
            <tbody>{(data?.rows ?? []).map((s: any) => (
              <tr key={s.id} className={selected === s.id ? "!bg-accent-soft" : ""}>
                <td className="max-w-md"><button type="button" onClick={() => router.replace(`/sources?id=${s.id}`)} className="text-left font-medium text-accent hover:underline">{s.title}</button><span className="mono block text-ink-3">{s.key}</span></td>
                <td className="whitespace-nowrap text-[12.5px]">{s.tier_label}</td><td className="num whitespace-nowrap">{day(s.retrieved_at)}</td><td className="r num">{s.n_results || "—"}</td>
              </tr>))}</tbody></table>
        </div>
      </div>
      <div>{selected ? <SourceDetail key={selected} id={selected} /> : <Callout tone="info" title="Select a source">Every source records its tier, retrieval date, content hash and versions. Registry records, FDA labels and FAERS counts are stored in full; abstracts, press releases and review documents are represented by a hash and the short quotes that anchor curated values.</Callout>}</div>
    </div>
  );
}

function ChangesTab() {
  const { data: changes } = useApi<any[]>("/changes");
  const { data: queue, reload } = useApi("/review-queue?limit=120");
  const [token, setToken] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const decide = (id: number, decision: string) => {
    setMsg(null);
    apiPost(`/review-queue/${id}/decision`, { decision, reviewer, rationale: `${titleCase(decision)} recorded from the review queue.` }, "POST", { "X-Reviewer-Token": token }).then(() => { setMsg("Decision recorded and written to the audit log."); reload(); }).catch((e) => setMsg(e.message));
  };
  return (
    <>
      {(changes ?? []).map((c) => (
        <Panel key={c.id} pad={false} className="mb-5">
          <div className="px-4 pt-3.5">
            {c.is_synthetic ? <Callout tone="warn" title="Synthetic demonstration">{c.synthetic_notice}</Callout> : null}
            <p className="mt-2 text-[14px] font-semibold text-ink">{c.source.title}</p>
            <p className="text-[12.5px] text-ink-3">Detected {day(c.detected_at)} · significance {c.significance.toLowerCase()} · review status {titleCase(c.review_status)}</p>
            <p className="mt-1.5 text-[13px] text-ink-2">{c.summary}</p>
          </div>
          <div className="mt-2 overflow-x-auto"><table className="tbl"><thead><tr><th>Field</th><th>Before</th><th>After</th><th>Significance</th><th>Why it matters</th></tr></thead>
            <tbody>{c.changes.map((x: any, i: number) => <tr key={i}><td className="font-medium">{x.label}</td><td className="max-w-xs text-ink-2">{Array.isArray(x.before) ? x.before.join("; ") : String(x.before ?? "—")}</td><td className="max-w-xs text-ink-2">{Array.isArray(x.after) ? x.after.join("; ") : String(x.after ?? "—")}</td><td><Badge tone={x.significance === "HIGH" ? "critical" : x.significance === "MEDIUM" ? "warn" : "muted"} icon={false}>{titleCase(x.significance)}</Badge></td><td className="text-ink-3">{x.note}</td></tr>)}</tbody></table></div>
        </Panel>
      ))}
      {changes && changes.length === 0 ? <Callout tone="muted" title="No source has changed since the last ingestion." >Run <span className="mono">incretinos check-updates --live</span> to re-fetch tracked records.</Callout> : null}
      <Panel className="mb-4">
        <p className="text-[13.5px] font-semibold">Manual review queue ({queue?.total ?? "…"} open)</p>
        <p className="mt-0.5 text-[12.5px] text-ink-3">{queue?.review_enabled ? "Recording a decision requires the reviewer token and your name; each decision is written to the audit log." : "This deployment is read-only: no reviewer token is configured, so decisions cannot be recorded."}</p>
        {queue?.review_enabled ? <div className="mt-2.5 flex flex-wrap gap-3"><label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">Reviewer name<input value={reviewer} onChange={(e) => setReviewer(e.target.value)} className="h-9 w-56 rounded-md border border-hairline-strong px-2.5 text-[13.5px] font-normal" /></label><label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">Reviewer token<input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} className="h-9 w-56 rounded-md border border-hairline-strong px-2.5 text-[13.5px] font-normal" /></label></div> : null}
        {msg ? <p role="status" className="mt-2 text-[12.5px] text-ink-2">{msg}</p> : null}
      </Panel>
      <div className="max-h-[520px] overflow-auto rounded-lg border border-hairline bg-surface">
        <table className="tbl"><thead><tr><th>Type</th><th>Item</th><th>Reason</th><th>Priority</th><th>Decision</th></tr></thead>
          <tbody>{(queue?.rows ?? []).map((r: any) => (
            <tr key={r.id}><td className="whitespace-nowrap">{titleCase(r.item_type)}</td><td className="max-w-md">{r.title}{r.payload?.quote ? <span className="block text-[12px] text-ink-3">“{r.payload.quote}”</span> : null}</td><td className="max-w-sm text-ink-3">{r.reason}</td><td>{titleCase(r.priority)}</td>
              <td className="whitespace-nowrap">{queue.review_enabled ? <span className="flex gap-1.5"><Button variant="ghost" disabled={!token || reviewer.length < 2} onClick={() => decide(r.id, "APPROVE")}>Approve</Button><Button variant="ghost" disabled={!token || reviewer.length < 2} onClick={() => decide(r.id, "REJECT")}>Reject</Button></span> : <span className="text-ink-3">read-only</span>}</td></tr>))}</tbody></table>
      </div>
    </>
  );
}

function AuditTab() {
  const { data } = useApi<any[]>("/audit?limit=300");
  return (
    <div className="max-h-[720px] overflow-auto rounded-lg border border-hairline bg-surface">
      <table className="tbl"><thead><tr><th>Time (UTC)</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
        <tbody>{(data ?? []).map((e) => <tr key={e.id}><td className="num whitespace-nowrap">{e.at.slice(0, 19).replace("T", " ")}</td><td>{e.actor}</td><td className="mono">{e.action}</td><td className="mono">{e.target_type}{e.target_id ? `:${e.target_id}` : ""}</td><td className="mono max-w-lg truncate text-ink-3" title={JSON.stringify(e.detail)}>{JSON.stringify(e.detail)}</td></tr>)}</tbody></table>
    </div>
  );
}

function ValidationTab() {
  const { data } = useApi("/validation");
  if (!data) return <Loading rows={8} />;
  return (
    <>
      <p className="mb-3 text-[13px] text-ink-2"><span className="num font-semibold text-ink">{data.passed} of {data.total}</span> scientific validation rules pass on the current evidence base. Each rule is also exercised with deliberately corrupted evidence in the test-suite.</p>
      <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
        <table className="tbl"><thead><tr><th>Rule</th><th>What it guarantees</th><th className="r">Items checked</th><th>Result</th></tr></thead>
          <tbody>{data.rules.map((r: any) => <tr key={r.rule_id}><td className="mono">{r.rule_id}</td><td>{r.title}{r.detail ? <span className="block text-[12px] text-ink-3">{r.detail}</span> : null}{r.violations.map((v: string) => <span key={v} className="block text-[12px] text-[var(--critical)]">{v}</span>)}</td><td className="r num">{r.checked.toLocaleString()}</td><td><Badge tone={r.passed ? "ok" : "critical"}>{r.passed ? "Pass" : "Fail"}</Badge></td></tr>)}</tbody></table>
      </div>
    </>
  );
}

function Explorer() {
  const params = useSearchParams();
  const tab = params.get("tab") ?? "sources";
  return (
    <>
      <PageHeader eyebrow="Source and audit explorer" title="Where every number came from, and what has changed" lede="Sources are hashed and versioned, curated quotes are anchored to source text, changes are queued for review, and every action is logged." />
      <Tabs.Root defaultValue={tab}>
        <Tabs.List className="mb-5 flex gap-5 overflow-x-auto border-b border-hairline" aria-label="Provenance views">
          {[["sources", "Sources"], ["changes", "Changes and review queue"], ["audit", "Audit log"], ["validation", "Validation rules"]].map(([v, l]) => <Tabs.Trigger key={v} value={v} className="-mb-px shrink-0 border-b-2 border-transparent pb-2 text-[13.5px] font-medium text-ink-3 data-[state=active]:border-accent data-[state=active]:text-ink">{l}</Tabs.Trigger>)}
        </Tabs.List>
        <Tabs.Content value="sources"><SourcesTab /></Tabs.Content>
        <Tabs.Content value="changes"><ChangesTab /></Tabs.Content>
        <Tabs.Content value="audit"><AuditTab /></Tabs.Content>
        <Tabs.Content value="validation"><ValidationTab /></Tabs.Content>
      </Tabs.Root>
    </>
  );
}

export default function Page() {
  return <Suspense fallback={<Loading />}><Explorer /></Suspense>;
}
