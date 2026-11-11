"use client";

import { Callout, TableCard, Term } from "@/components/ui";
import { type LlmMeta, VERDICT_LABEL, type WithheldSentence } from "@/lib/assistant";
import { MarkerLink } from "./AnswerBlock";

const show = (v: unknown): string => (v === null || v === undefined ? "Not stated" : typeof v === "string" ? v : JSON.stringify(v));

/**
 * Why a model-drafted summary was withheld. The API never returns the withheld sentences (they may carry injected
 * or simply false text): only, per sentence, the verdict, the markers it cited, the reasons and, per claim, the field
 * that failed with the claimed and the stored value. That is all this shows.
 */
function WithheldTable({ sentences, known }: { sentences: WithheldSentence[]; known: Set<string> }) {
  return (
    <TableCard label="Claim verification of the withheld draft, sentence by sentence" maxHeight="none" className="mt-2">
      <table className="tbl tbl-compact" style={{ minWidth: 640 }} data-testid="withheld-sentences">
        <caption className="sr-only">Per-sentence verdicts of the withheld model draft. The sentences themselves are not returned.</caption>
        <thead>
          <tr>
            <th scope="col">Sentence</th>
            <th scope="col">Verdict</th>
            <th scope="col">Cited</th>
            <th scope="col">Reasons and mismatched fields</th>
          </tr>
        </thead>
        <tbody>
          {sentences.map((s) => (
            <tr key={s.index}>
              <th scope="row" className="num font-medium text-ink">
                {s.index}
              </th>
              <td className="whitespace-nowrap">{VERDICT_LABEL[s.verdict] ?? s.verdict}</td>
              <td className="whitespace-nowrap">{s.cited.length ? s.cited.map((c) => (known.has(c) ? <MarkerLink key={c} id={c} /> : <span key={c} className="mono text-critical">[{c}]</span>)) : <span className="text-ink-3">None</span>}</td>
              <td className="min-w-[280px]">
                {s.reasons.length ? (
                  <ul className="list-disc space-y-0.5 pl-4">
                    {s.reasons.map((r) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                ) : (
                  <span className="text-ink-3">No reason given</span>
                )}
                {s.claims.flatMap((c) => c.mismatches).length ? (
                  <ul className="mt-1 space-y-0.5 text-caption text-ink-2">
                    {s.claims.flatMap((c, ci) =>
                      c.mismatches.map((m, mi) => (
                        <li key={`${ci}:${mi}`}>
                          <span className="mono" translate="no">
                            {m.field}
                          </span>
                          : the draft says {show(m.claimed)}; the record says {show(m.actual)}
                          {c.bound_card ? <> (checked against <MarkerLink id={c.bound_card} />)</> : null}
                        </li>
                      )),
                    )}
                  </ul>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableCard>
  );
}

/** Whether a model was used for this answer, and what claim verification decided. Says nothing it was not told. */
export function ModelReport({ llm, known }: { llm: LlmMeta; known: Set<string> }) {
  const check = llm.citation_check;
  const verdicts = check?.verdicts ? Object.entries(check.verdicts) : [];
  if (llm.rejected) {
    const all: unknown[] = check?.sentences ?? [];
    const sentences = all.filter((s): s is WithheldSentence => !!s && typeof (s as WithheldSentence).verdict === "string" && Array.isArray((s as WithheldSentence).reasons) && Array.isArray((s as WithheldSentence).claims));
    return (
      <div data-testid="model-withheld">
        <Callout tone="warn" title="Model-drafted summary withheld" level={3}>
          {llm.error === "VerificationError" ? (
            <p>The claim verifier could not read the draft, so it is withheld. The answer above is composed from stored evidence without a model.</p>
          ) : (
            <p>
              The draft did not pass <Term k="claim-verification">claim verification</Term>, so it is not shown. The answer above is composed from stored evidence without a model.
              {check ? ` ${check.sentences_checked} sentences checked.` : ""}
              {verdicts.length ? ` ${verdicts.map(([v, n]) => `${VERDICT_LABEL[v] ?? v}: ${n}`).join("; ")}.` : ""}
            </p>
          )}
          {check?.method ? <p className="mt-1 text-caption">Method: {check.method}.</p> : null}
        </Callout>
        {sentences.length ? <WithheldTable sentences={sentences} known={known} /> : null}
      </div>
    );
  }
  if (llm.used) {
    return (
      <p className="text-data text-ink-3" data-testid="model-used">
        A model ({llm.provider}) drafted the summary above; every sentence passed <Term k="claim-verification">claim verification</Term>
        {verdicts.length ? ` (${verdicts.map(([v, n]) => `${VERDICT_LABEL[v] ?? v}: ${n}`).join("; ")})` : ""}.
        {llm.cards?.truncated ? ` It was shown ${llm.cards.offered} of ${llm.cards.available} evidence cards.` : ""}
      </p>
    );
  }
  if (llm.error) {
    return <p className="text-data text-ink-3">No model summary: the model did not answer ({llm.error}). The answer is composed from stored evidence without a model.</p>;
  }
  return null;
}
