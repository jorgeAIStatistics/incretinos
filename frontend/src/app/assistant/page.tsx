"use client";

import clsx from "clsx";
import { Ban, BookCheck, CircleSlash, FlaskConical, Lightbulb, Sparkles } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Callout, ErrorNote, Loading, PageHeader, Panel, SourceLine, TierBadge, VerificationBadge } from "@/components/ui";
import { apiPost, useApi } from "@/lib/api";

const KIND: Record<string, { label: string; Icon: any; cls: string }> = {
  REPORTED_EVIDENCE: { label: "Reported evidence", Icon: BookCheck, cls: "border-l-[var(--teal)]" },
  INTERPRETATION: { label: "Rule-based interpretation", Icon: Lightbulb, cls: "border-l-[var(--accent)]" },
  UNAVAILABLE: { label: "Not available in stored sources", Icon: CircleSlash, cls: "border-l-[var(--ink-3)]" },
  HYPOTHESIS: { label: "Hypothesis — untested", Icon: FlaskConical, cls: "border-l-[#c9b37a] border-dashed" },
  REFUSAL: { label: "Not answered", Icon: Ban, cls: "border-l-[var(--critical)]" },
  MODEL_SUMMARY: { label: "Model-drafted summary (every sentence checked against its cited evidence)", Icon: Sparkles, cls: "border-l-[var(--series-2)]" },
};

/**
 * How far the records behind a block have been checked, in the shared evidence-state language: one chip per distinct
 * stored status (a block can rest on several records), then the tier of its source. "Reported evidence" says only
 * that a source reports it; nothing here reads as verified unless every record behind the block is HUMAN_VERIFIED,
 * which is the one status the badge draws in green.
 */
function BlockVerification({ block }: { block: any }) {
  const v = block.verification;
  const statuses: string[] = v ? Array.from(new Set<string>(v.statuses ?? [])) : [];
  if (!statuses.length && block.kind !== "REPORTED_EVIDENCE") return null;
  return (
    <span className="flex flex-wrap items-center gap-1" data-testid="block-verification">
      {statuses.length ? (
        statuses.map((status) => <VerificationBadge key={status} status={status} compact />)
      ) : (
        <Badge tone="muted" icon={false} title="This block states registry record facts or rule-based text; no reviewable evidence record is behind it.">
          No review status
        </Badge>
      )}
      {statuses.length > 1 ? <span className="text-caption font-normal text-ink-3">{v.records?.length ?? statuses.length} records; not all at the same stage</span> : null}
      {block.tier ? <TierBadge tier={block.tier} /> : null}
    </span>
  );
}

export default function Assistant() {
  const examplesRequest = useApi<string[]>("/assistant/examples");
  const examples = examplesRequest.data;
  const metaRequest = useApi("/meta");
  const meta = metaRequest.data;
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ask = (q: string) => {
    const text = q.trim();
    if (text.length < 3) return;
    setQuestion(text); setBusy(true); setError(null);
    apiPost("/assistant/ask", { question: text }).then(setAnswer).catch((e) => { setAnswer(null); setError(e.message); }).finally(() => setBusy(false));
  };
  const index = new Map<number, number>((answer?.sources ?? []).map((s: any, i: number) => [s.id, i + 1]));
  return (
    <>
      <PageHeader eyebrow="Research assistant" title="Answers assembled from stored evidence, with every claim cited" lede="The assistant retrieves structured evidence first and composes its answer from it. It names what the sources do not contain, marks hypotheses, and declines treatment recommendations and unsupported causal claims." />
      {/* Whether a model is used is a fact reported by the service. Until it is known — or if it cannot be asked — nothing is claimed either way. */}
      {metaRequest.status === "error" ? (
        <ErrorNote message={`Whether a language model is configured could not be determined: the service status did not load. ${metaRequest.error ?? ""}`} onRetry={metaRequest.reload} />
      ) : !meta ? (
        <p role="status" className="text-data text-ink-3">Checking whether a language model is configured…</p>
      ) : (
      <Callout tone={meta.llm.enabled ? "info" : "muted"} title={meta.llm.enabled ? `Language model enabled (${meta.llm.provider})` : "No language model is configured — answers are fully deterministic"}>
        {meta.llm.enabled ? "A model drafts an optional prose summary from the evidence cards below. Each sentence is read into structured claims and checked, without a model, against the database record behind the card it cites (drug, arm, trial, endpoint, value, unit, time point, estimand, interval, tier and verification status). The draft is shown only if every sentence is supported; otherwise it is withheld." : "Retrieval, routing and interpretation are rule-based. Setting INCRETINOS_LLM_PROVIDER adds an optional model-drafted summary; it is shown only if every sentence is supported by the record behind the card it cites."}
      </Callout>
      )}
      <form className="mt-5 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); ask(question); }}>
        <label className="sr-only" htmlFor="q">Research question</label>
        <input id="q" value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={1000} placeholder="Ask about the evidence, e.g. what differentiates retatrutide from tirzepatide?" className="h-10 min-w-0 flex-1 rounded-md border border-hairline-strong bg-surface px-3 text-[14px]" />
        <Button type="submit" disabled={busy || question.trim().length < 3}>Ask</Button>
      </form>
      {examplesRequest.status === "error" ? <div className="mt-3"><ErrorNote message={`The example questions could not be loaded. You can still type a question. ${examplesRequest.error ?? ""}`} onRetry={examplesRequest.reload} /></div> : null}
      <div className="mt-3 flex flex-wrap gap-2">
        {(examples ?? []).map((ex) => <button key={ex} type="button" onClick={() => ask(ex)} className="rounded-full border border-hairline-strong bg-surface px-3 py-1 text-[12.5px] text-ink-2 hover:border-accent hover:text-accent">{ex}</button>)}
      </div>
      {error ? <div className="mt-5"><ErrorNote message={error} /></div> : null}
      {busy ? <div className="mt-6"><Loading rows={4} /></div> : null}
      {answer && !busy ? (
        <div className="fade-in mt-6 grid gap-6 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]" data-testid="assistant-answer">
          <div>
            <h2 className="mb-1 text-title font-semibold text-ink">Answer</h2>
            <p className="mb-2 text-[12px] text-ink-3">Question type: <span className="mono">{answer.intent}</span>{answer.llm.rejected ? <span className="ml-2"><Badge tone="warn">Model draft withheld by claim verification</Badge></span> : null}{answer.llm.cards?.truncated ? <span className="ml-2"><Badge tone="warn">Model saw {answer.llm.cards.offered} of {answer.llm.cards.available} evidence cards</Badge></span> : null}</p>
            <ol className="space-y-2.5">
              {answer.blocks.map((b: any, i: number) => { const k = KIND[b.kind] ?? KIND.INTERPRETATION; return (
                <li key={i} id={b.card_id ? `card-${b.card_id}` : undefined} className={clsx("rounded-r-md border border-l-4 border-hairline bg-surface px-4 py-3", k.cls)}>
                  <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3"><h3 className="flex items-center gap-1.5 font-semibold text-ink-2"><k.Icon aria-hidden size={13} /> {k.label}</h3>{b.card_id ? <span className="mono rounded border border-hairline-strong px-1" title="Marker a model-drafted summary uses to cite this block">{b.card_id}</span> : null}<BlockVerification block={b} /></div>
                  <p className="text-[13.5px] leading-relaxed text-ink">{b.text}</p>
                  {b.note ? <p className="mt-1 text-[12px] text-ink-3">{b.note}</p> : null}
                  {b.citations.length ? <p className="mt-1.5 text-[12px] text-ink-3">Sources: {b.citations.map((id: number) => <Link key={id} href={`/sources?id=${id}`} className="num mr-1.5 font-medium">[{index.get(id) ?? id}]</Link>)}{b.result_ids.length ? <Link href={`/compare?a=${b.result_ids[0]}`} className="ml-1">open finding →</Link> : null}</p> : null}
                </li>); })}
            </ol>
            {answer.llm.citation_check && !answer.llm.citation_check.passed ? <div className="mt-3"><Callout tone="warn" title="Why the model draft was withheld"><ul className="list-disc pl-5">{answer.llm.citation_check.problems.map((p: string) => <li key={p}>{p}</li>)}</ul></Callout></div> : null}
          </div>
          <Panel className="h-fit" title={`Sources cited (${answer.sources.length})`} level={2}>
            <ol className="space-y-2.5">
              {answer.sources.map((s: any, i: number) => <li key={s.id} className="flex gap-2 text-[12.5px]"><span className="num font-semibold text-ink-3">[{i + 1}]</span><SourceLine source={s} /></li>)}
              {answer.sources.length === 0 ? <li className="text-[12.5px] text-ink-3">No sources: nothing factual was asserted.</li> : null}
            </ol>
          </Panel>
        </div>
      ) : null}
    </>
  );
}
