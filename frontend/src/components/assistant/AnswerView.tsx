"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { SourceLine, Term, VerificationBadge } from "@/components/ui";
import { type Answer, blockAnchor, INTENT_LABEL, nctIds } from "@/lib/assistant";
import type { DrugSummary } from "@/lib/types";
import { AnswerBlockView, kindLook, MarkerLink } from "./AnswerBlock";
import { ModelReport } from "./ModelReport";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** Molecules the question names, by name, code or brand, matched against the molecule list. Display only. */
function namedMolecules(question: string, drugs: DrugSummary[]): DrugSummary[] {
  const q = question.toLowerCase();
  return drugs.filter((d) => [d.name, d.code_name, ...(d.brand_names ?? [])].some((n) => !!n && new RegExp(`\\b${n.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`).test(q)));
}

/** The answer: what was asked, how it was read, its blocks in the order the API composed them, then what it cannot tell you. */
export function AnswerMain({ answer }: { answer: Answer }) {
  const sourceNumber = new Map(answer.sources.map((s, i) => [s.id, i + 1]));
  const markers = new Set(Object.keys(answer.markers ?? {}));
  const kinds = new Map<string, number>();
  for (const b of answer.blocks) kinds.set(b.kind, (kinds.get(b.kind) ?? 0) + 1);
  const waiting = answer.blocks.filter((b) => b.verification?.needs_attention).length;
  const firstOf = (kind: string) => answer.blocks.findIndex((b) => b.kind === kind);
  return (
    <div className="min-w-0">
      <h2 className="text-title font-semibold text-ink">Answer</h2>
      <p className="mt-0.5 text-data text-ink-3">
        Read as: <span className="text-ink-2">{INTENT_LABEL[answer.intent] ?? answer.intent}</span>{" "}
        <span className="mono" translate="no">
          {answer.intent}
        </span>
      </p>
      <nav aria-label="Blocks of this answer by kind" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-data" data-testid="answer-kinds">
        {Array.from(kinds.entries()).map(([kind, n]) => {
          const i = firstOf(kind);
          const look = kindLook(kind);
          return (
            <a key={kind} href={`#${blockAnchor(answer.blocks[i], i)}`} className="inline-flex items-center gap-1 font-medium">
              <look.Icon aria-hidden size={13} className="shrink-0" />
              {n} {look.label.toLowerCase()}
            </a>
          );
        })}
      </nav>
      {waiting ? (
        <p className="mt-1.5 text-data text-warn" data-testid="answer-attention">
          {plural(waiting, "block rests", "blocks rest")} on at least one record that waits for a person (pending human review or recheck required).
        </p>
      ) : null}

      <ol className="mt-4 space-y-3">
        {answer.blocks.map((b, i) => (
          <li key={i}>
            <AnswerBlockView block={b} index={i} sourceNumber={sourceNumber} markers={markers} />
          </li>
        ))}
      </ol>

      <div className="mt-4">
        <ModelReport llm={answer.llm} known={markers} />
      </div>

      <section aria-labelledby="limits" className="mt-6 border-t border-hairline pt-4" data-testid="answer-limitations">
        <h2 id="limits" className="text-title font-semibold text-ink">
          What this answer cannot tell you
        </h2>
        <ul className="mt-2 max-w-[78ch] list-disc space-y-1 pl-5 text-data text-ink-2">
          <li>It is composed only from ingested public sources. Something the stored sources do not contain is reported as not available, which is not evidence that it does not exist.</li>
          <li>
            Reported evidence states what a source reports. How far each record has been checked is shown beside it; only a reviewer&rsquo;s recorded decision makes a record{" "}
            <Term k="human-verified">human-verified</Term>.
          </li>
          <li>
            Values from different trials are not a randomized comparison. Where the answer sets two results side by side, the <Term k="rule-based-interpretation">rule-based interpretation</Term> says what may and may not be concluded.
          </li>
          <li>Hypotheses are marked as such and are not evidence. The assistant gives no treatment recommendation.</li>
          {answer.llm.cards?.truncated ? <li>The model was shown {answer.llm.cards.offered} of {answer.llm.cards.available} evidence cards.</li> : null}
        </ul>
        <p className="mt-2 text-caption text-ink-3">{answer.llm.reason ? `Model: ${answer.llm.reason}.` : `Model: ${answer.llm.provider}.`}</p>
      </section>
    </div>
  );
}

/** Beside the answer: the sources cited, the citation markers resolved, and where to go next. */
export function AnswerRail({ answer, drugs }: { answer: Answer; drugs: DrugSummary[] }) {
  const markerIds = Object.keys(answer.markers ?? {});
  const results = Array.from(new Set(answer.blocks.flatMap((b) => b.result_ids)));
  const trials = Array.from(new Set(answer.blocks.flatMap((b) => nctIds(b.text))));
  const molecules = namedMolecules(answer.question, drugs);
  const sourceNumber = new Map(answer.sources.map((s, i) => [s.id, i + 1]));
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <section aria-labelledby="sources-cited">
        <h2 id="sources-cited" className="text-body font-semibold text-ink">
          Sources cited ({answer.sources.length})
        </h2>
        {answer.sources.length === 0 ? (
          <p className="mt-2 text-data text-ink-3">No sources: nothing factual was asserted.</p>
        ) : (
          <ol className="mt-2 space-y-2.5">
            {answer.sources.map((s, i) => (
              <li key={s.id} id={`source-${i + 1}`} className="answer-source flex gap-2 rounded-chip text-data" data-testid="cited-source">
                <span className="num shrink-0 font-semibold text-ink-3">[{i + 1}]</span>
                <SourceLine source={s} />
              </li>
            ))}
          </ol>
        )}
      </section>

      {markerIds.length ? (
        <section aria-labelledby="markers-heading">
          <h2 id="markers-heading" className="text-body font-semibold text-ink">
            <Term k="citation-marker">Citation markers</Term> ({markerIds.length})
          </h2>
          <table className="mt-2 w-full border-collapse text-data" data-testid="marker-index">
            <caption className="sr-only">Each citation marker, the kind of block it names, how far its records have been checked and its sources</caption>
            <thead className="sr-only">
              <tr>
                <th scope="col">Marker</th>
                <th scope="col">Block and verification</th>
                <th scope="col">Sources</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {markerIds.map((id) => {
                const mk = answer.markers[id];
                const block = answer.blocks[mk.block];
                const statuses = Array.from(new Set(block?.verification?.statuses ?? []));
                return (
                  <tr key={id} className="align-top">
                    <th scope="row" className="w-12 py-1.5 pr-2 text-left font-normal">
                      <MarkerLink id={id} />
                    </th>
                    <td className="py-1.5 pr-2">
                      <span className="block text-ink-2">{kindLook(mk.kind).label}</span>
                      {statuses.length ? (
                        <span className="mt-0.5 flex flex-wrap gap-1">
                          {statuses.map((s) => (
                            <VerificationBadge key={s} status={s} compact explain={false} />
                          ))}
                        </span>
                      ) : null}
                    </td>
                    <td className="num w-16 py-1.5 text-right text-caption">
                      {mk.source_ids.length ? (
                        mk.source_ids.map((sid) => (
                          <a key={sid} href={`#source-${sourceNumber.get(sid) ?? sid}`} className="ml-1">
                            [{sourceNumber.get(sid) ?? sid}]
                          </a>
                        ))
                      ) : (
                        <span className="text-ink-3">None</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ) : null}

      {results.length || trials.length || molecules.length ? (
        <section aria-labelledby="explore-heading" data-testid="explore-further">
          <h2 id="explore-heading" className="text-body font-semibold text-ink">
            Explore further
          </h2>
          <ul className="mt-2 space-y-1.5 text-data">
            {results.length >= 2 ? (
              <li>
                <Link href={`/compare?a=${results[0]}&b=${results[1]}`} className="inline-flex items-center gap-1 font-medium" data-testid="compare-findings">
                  Compare the first two reported findings <ArrowRight aria-hidden size={13} />
                </Link>
                <span className="block text-caption text-ink-3">Opens in the comparison laboratory with its classification and the reasons for it.</span>
              </li>
            ) : null}
            {trials.map((nct) => (
              <li key={nct}>
                <Link href={`/trials/${nct}`} className="inline-flex items-center gap-1 font-medium">
                  Trial <span className="mono" translate="no">{nct}</span> <ArrowRight aria-hidden size={13} />
                </Link>
              </li>
            ))}
            {molecules.map((d) => (
              <li key={d.slug}>
                <Link href={`/molecules?drug=${d.slug}`} className="font-medium">
                  {d.name}
                </Link>
                <span className="text-ink-3"> in the molecule explorer, or its </span>
                <Link href={`/gaps?drug=${d.slug}`}>open evidence questions</Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
