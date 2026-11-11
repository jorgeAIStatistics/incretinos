"use client";

import { ArrowRight, CircleDashed, Info } from "lucide-react";
import { type FormEvent, Suspense, useState } from "react";
import { AnswerMain, AnswerRail } from "@/components/assistant/AnswerView";
import { Async, Button, Disclosure, ErrorNote, Loading, PageHeader, Skeleton, Term, TextField } from "@/components/ui";
import { type ApiState, apiPost, useApi, useResource } from "@/lib/api";
import type { Answer } from "@/lib/assistant";
import { useQueryParams } from "@/lib/hooks";
import { rowsOf } from "@/lib/paging";
import type { DrugSummary, Meta } from "@/lib/types";

/*
 * Research Assistant. The question lives in the address (?q=), so an answer can be linked to and reloaded. The
 * answer is the API's: typed blocks in the order it composed them, each with its verification label, its sources and
 * its [C#] marker; this page only lays them out and adds the links the answer already implies.
 */

const MAX_QUESTION = 1000;

/** Whether a model is used is a fact reported by the service. Until it is known, or if it cannot be asked, nothing is claimed either way. */
function ModelStatus({ meta }: { meta: ApiState<Meta> }) {
  if (meta.status === "error") return <ErrorNote message={`Whether a language model is configured could not be determined: the service status did not load. ${meta.error ?? ""}`} onRetry={meta.reload} />;
  if (!meta.data) return <p role="status" className="text-data text-ink-3">Checking whether a language model is configured…</p>;
  const llm = meta.data.llm;
  return (
    <p className="flex items-start gap-1.5 text-data text-ink-2" data-testid="model-status">
      {llm.enabled ? <Info aria-hidden size={14} className="mt-[3px] shrink-0 text-accent" /> : <CircleDashed aria-hidden size={14} className="mt-[3px] shrink-0 text-ink-3" />}
      <span>
        {llm.enabled ? (
          <>
            <span className="font-medium text-ink">Language model enabled ({llm.provider}).</span> It may draft a summary from the evidence blocks; the summary is shown only if{" "}
            <Term k="claim-verification">claim verification</Term> supports every sentence.
          </>
        ) : (
          <>
            <span className="font-medium text-ink">No language model is configured. Answers are composed deterministically</span>, by rules, from stored evidence.
          </>
        )}
      </span>
    </p>
  );
}

function ExampleList({ examples, onAsk }: { examples: string[]; onAsk: (q: string) => void }) {
  return (
    <ul className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2" data-testid="example-questions">
      {examples.map((ex) => (
        <li key={ex}>
          <button type="button" onClick={() => onAsk(ex)} className="group flex min-h-8 w-full items-start gap-1.5 rounded-chip py-1 text-left text-data text-accent">
            <ArrowRight aria-hidden size={13} className="mt-[3px] shrink-0 text-ink-3 transition-transform duration-(--dur-fast) ease-standard group-hover:translate-x-0.5 group-hover:text-accent" />
            <span className="group-hover:underline">{ex}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function Method() {
  return (
    <section aria-labelledby="method-heading">
      <h2 id="method-heading" className="text-body font-semibold text-ink">
        How an answer is built
      </h2>
      <ul className="mt-2 space-y-2 text-data text-ink-2">
        <li>Structured evidence is retrieved from the stored sources first; nothing outside them is used.</li>
        <li>
          The answer is composed as typed blocks: reported evidence, <Term k="rule-based-interpretation">rule-based interpretation</Term>, what the sources do not contain, and
          hypotheses, each kept visibly apart.
        </li>
        <li>Each block carries the verification status of the records behind it and a <Term k="citation-marker">citation marker</Term> that resolves to its sources and records.</li>
        <li>Treatment recommendations and causal claims from spontaneous reports are declined.</li>
      </ul>
    </section>
  );
}

function AnswerSkeleton() {
  return (
    <div role="status" aria-label="Composing the answer from stored evidence…" className="space-y-3">
      <Skeleton className="h-6 w-32" />
      {Array.from({ length: 4 }, (_, i) => (
        <Skeleton key={i} className="h-24" />
      ))}
      <span className="sr-only">Composing the answer from stored evidence…</span>
    </div>
  );
}

function Assistant() {
  const [param, setParams] = useQueryParams();
  const asked = param("q").trim();
  const [draft, setDraft] = useState(asked);
  const [invalid, setInvalid] = useState<string | null>(null);
  const examples = useApi<string[]>("/assistant/examples");
  const meta = useApi<Meta>("/meta");
  const drugs = useApi<unknown>("/drugs");
  const answer = useResource<Answer>(asked.length >= 3 ? `ask:${asked}` : null, (signal) => apiPost<Answer>("/assistant/ask", { question: asked }, "POST", {}, signal));

  const ask = (q: string) => {
    const text = q.trim();
    if (text.length < 3) {
      setInvalid("Type a question of at least three characters.");
      return;
    }
    setInvalid(null);
    setDraft(text);
    if (text === asked) answer.reload();
    else setParams({ q: text });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask(draft);
  };

  const exampleList =
    examples.status === "error" ? (
      <ErrorNote message={`The example questions could not be loaded. You can still type a question. ${examples.error ?? ""}`} onRetry={examples.reload} />
    ) : !examples.data ? (
      <Loading rows={2} label="Loading example questions…" />
    ) : (
      <ExampleList examples={examples.data} onAsk={ask} />
    );

  return (
    <>
      <PageHeader
        eyebrow="Research assistant"
        title="Answers assembled from stored evidence, with every claim cited"
        lede="Structured evidence is retrieved first and the answer is composed from it. It names what the sources do not contain, marks hypotheses, and declines treatment recommendations and unsupported causal claims."
      />
      <div className="grid grid-cols-1 gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0">
          <form onSubmit={submit} noValidate className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <TextField
              label="Research question"
              name="question"
              value={draft}
              onChange={(v) => {
                setDraft(v);
                if (invalid) setInvalid(null);
              }}
              maxLength={MAX_QUESTION}
              placeholder="What differentiates retatrutide from tirzepatide…"
              error={invalid}
              className="flex-1"
            />
            <Button type="submit" loading={answer.status === "loading"} className="shrink-0">
              {answer.status === "loading" ? "Asking…" : "Ask"}
            </Button>
          </form>
          <div className="mt-2">
            <ModelStatus meta={meta} />
          </div>

          <section aria-labelledby={asked ? undefined : "examples-heading"} aria-label={asked ? "Example questions" : undefined} className="mt-5">
            {asked ? (
              <Disclosure summary={`Example questions${examples.data ? ` (${examples.data.length})` : ""}`}>{exampleList}</Disclosure>
            ) : (
              <>
                <h2 id="examples-heading" className="mb-1.5 text-body font-semibold text-ink">
                  Example questions
                </h2>
                {exampleList}
              </>
            )}
          </section>

          <div className="mt-6" data-testid="assistant-answer-area">
            <Async state={answer} errorTitle="The question could not be answered" loading={<AnswerSkeleton />}>
              {(a) => (
                <div data-testid="assistant-answer">
                  <p role="status" className="sr-only">
                    Answer ready: {a.blocks.length} blocks, {a.sources.length} sources cited.
                  </p>
                  <AnswerMain answer={a} />
                </div>
              )}
            </Async>
          </div>
        </div>

        <aside aria-label="Sources, markers and next steps" className="min-w-0 lg:border-l lg:border-hairline lg:pl-8">
          {answer.status === "success" && answer.data ? (
            <div data-testid="assistant-rail">
              <AnswerRail answer={answer.data} drugs={rowsOf<DrugSummary>(drugs.data)} />
            </div>
          ) : (
            <Method />
          )}
        </aside>
      </div>
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={6} label="Loading the research assistant…" />}>
      <Assistant />
    </Suspense>
  );
}
