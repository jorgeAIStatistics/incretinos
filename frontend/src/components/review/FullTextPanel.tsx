"use client";

import clsx from "clsx";
import { ExternalLink } from "lucide-react";
import { Callout, Disclosure, Missing } from "@/components/ui";
import { titleCase } from "@/lib/api";
import { type FullTextBlock, shortHash, stamp } from "@/lib/review";
import { Block, Fields, Stored } from "./bits";

/*
 * For a row read from a full-text document (origin FULLTEXT): the document and its licence as the source returned it,
 * where in the document the value is, the table exactly as it was extracted with the cells this row used marked, the
 * footnotes, the parsing method and the parser's own quality assessment. The assessment describes the reading; it is
 * not evidence that the value is right, and the panel says so.
 */

function locatorOf(ft: FullTextBlock): string | null {
  const t = ft.table;
  if (t) return t.locator;
  const p = ft.extraction?.passage as Record<string, unknown> | null | undefined;
  if (!p) return null;
  if (p.page != null) return `PDF page ${String(p.page)}${p.heading ? `, under “${String(p.heading)}”` : ""}`;
  return [p.section, p.paragraph_id ? `paragraph ${String(p.paragraph_id)}` : null].filter(Boolean).map(String).join(", ") || null;
}

function QualityValue({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return <Missing className="!whitespace-normal">Not stated</Missing>;
  if (Array.isArray(value)) return <>{value.map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v))).join("; ")}</>;
  if (typeof value === "object") {
    return (
      <dl className="grid grid-cols-1 gap-y-0.5">
        {Object.entries(value as Record<string, unknown>)
          .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0) && !(typeof v === "object" && !Array.isArray(v) && Object.keys(v as object).length === 0))
          .map(([k, v]) => (
            <div key={k} className="min-w-0">
              <dt className="inline text-ink-3">{titleCase(k.replace(/_/g, " "))}: </dt>
              <dd className="inline break-words">{typeof v === "object" ? (Array.isArray(v) ? v.map(String).join("; ") : JSON.stringify(v)) : String(v)}</dd>
            </div>
          ))}
      </dl>
    );
  }
  return <>{String(value)}</>;
}

function ExtractedTableView({ ft }: { ft: FullTextBlock }) {
  const t = ft.table!;
  const used = new Set((ft.used_cells ?? []).map(([r, c]) => `${r}:${c}`));
  const usedRows = new Set((ft.used_cells ?? []).map(([r]) => r));
  const usedCols = new Set([...(ft.used_cells ?? []).map(([, c]) => c), ...(ft.used_header_columns ?? [])]);
  const nCols = (t.n_columns ?? 0) + 1;
  const headerRows = t.header_rows ?? [];
  const body = t.body_rows ?? [];
  return (
    <div className="overflow-x-auto rounded-control border border-hairline" data-testid="fulltext-table">
      <table className="w-full border-collapse text-caption">
        <caption className="caption-top px-2 py-1.5 text-left text-caption text-ink-2">
          <span className="font-semibold text-ink">{t.caption}</span>
        </caption>
        <thead>
          {headerRows.map((row, ri) => (
            <tr key={ri} className="border-b border-hairline bg-sunken">
              {row.map((cell, ci) => (
                <th
                  key={ci}
                  scope="col"
                  colSpan={cell.colspan ?? 1}
                  className={clsx("px-2 py-1 text-left align-bottom font-semibold text-ink", usedCols.has(cell.col) && "bg-accent-soft underline decoration-accent decoration-2 underline-offset-[3px]")}
                >
                  {cell.text}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {body.map((row, ri) => (
            <tr key={ri} className={clsx("border-b border-hairline last:border-b-0", row.kind === "group" && "bg-sunken")} data-used-row={usedRows.has(ri) ? "true" : undefined}>
              <th scope="row" colSpan={row.kind === "group" ? nCols : 1} className={clsx("px-2 py-1 text-left align-top font-normal text-ink", row.kind === "group" && "font-semibold", (row.indent ?? 0) > 0 && "pl-5", usedRows.has(ri) && "font-semibold")}>
                {row.stub}
                {row.footnote_refs?.length ? <sup className="ml-0.5 text-ink-3">{row.footnote_refs.join(",")}</sup> : null}
              </th>
              {row.kind === "group"
                ? null
                : Array.from({ length: nCols - 1 }, (_, k) => k + 1).map((col) => {
                    const text = row.cells?.[String(col)];
                    const isUsed = used.has(`${ri}:${col}`);
                    return (
                      <td key={col} className="num px-2 py-1 align-top text-ink" data-used={isUsed ? "true" : undefined}>
                        {isUsed ? (
                          <mark className="rounded-[2px] bg-accent-soft px-0.5 font-semibold text-ink underline decoration-accent decoration-2 underline-offset-[3px]" title="Used by this record">
                            {text}
                          </mark>
                        ) : (
                          text ?? ""
                        )}
                      </td>
                    );
                  })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="border-t border-hairline px-2 py-1.5 text-caption text-ink-3">
        <mark className="rounded-[2px] bg-accent-soft px-0.5 font-semibold text-ink underline decoration-accent decoration-2 underline-offset-[3px]">Shaded and underlined</mark> cells (and headings) are the ones this record was read from.
      </p>
    </div>
  );
}

export function FullTextPanel({ ft }: { ft: FullTextBlock }) {
  const doc = ft.document ?? null;
  const t = ft.table ?? null;
  const x = ft.extraction ?? null;
  const locator = locatorOf(ft);
  const passage = x?.passage as Record<string, unknown> | null | undefined;
  const quality = Object.entries(x?.quality ?? {});
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="fulltext-panel">
      <Block title="Full-text document" level={3} testId="fulltext-document">
        {doc ? (
          <>
            <p className="text-data font-medium leading-snug text-ink">
              {doc.url ? (
                <a href={doc.url} target="_blank" rel="noreferrer noopener" className="text-accent hover:underline">
                  {doc.title}
                  <ExternalLink aria-hidden size={12} className="ml-1 inline align-baseline" />
                  <span className="sr-only"> (opens the original in a new tab)</span>
                </a>
              ) : (
                doc.title
              )}
            </p>
            <Fields
              className="mt-2"
              rows={[
                ["Kind", doc.kind === "PDF" ? "PDF (text layer)" : "JATS XML (Europe PMC)"],
                ["Identifiers", [doc.pmcid, doc.pmid ? `PMID ${doc.pmid}` : null, doc.doi ? `DOI ${doc.doi}` : null, doc.application_number].filter(Boolean).join(" · ") || <Missing key="i" />],
                ["Licence (as returned)", <span key="l" data-testid="fulltext-licence">{doc.licence ?? <Missing className="!whitespace-normal">No licence returned</Missing>}</span>],
                ["Full text stored", <span key="s" data-testid="fulltext-stored">{doc.full_text_stored ? "Yes" : "No"}</span>],
                ["Retrieved", stamp(doc.retrieved_at)],
                ["Document SHA-256", doc.content_hash ? <span key="h" className="mono break-all" title={doc.content_hash}>{shortHash(doc.content_hash, 16)}</span> : <Missing key="h" />],
                ["Fetch / parse", `${titleCase(doc.fetch_status)} / ${titleCase(doc.parse_status)}`],
                ["Pages", <Stored key="pg" value={doc.page_count} missing="Not applicable" />],
              ]}
            />
            {doc.licence_basis ? <p className="mt-2 text-caption text-ink-2">Licence basis: {doc.licence_basis}</p> : null}
            {doc.storage_basis ? <p className="mt-1 text-caption text-ink-3">What is kept: {doc.storage_basis}</p> : null}
            {doc.extraction_scope ? <p className="mt-1 text-caption text-ink-3">Extraction scope: {doc.extraction_scope}</p> : null}
            {doc.limitations?.length ? (
              <Disclosure summary={`Recorded limitations of this document (${doc.limitations.length})`} className="mt-2">
                <ul className="list-disc pl-4 text-caption text-ink-2">
                  {doc.limitations.map((l, i) => (
                    <li key={i}>
                      <span className="font-medium text-ink">{titleCase(l.code.replace(/_/g, " "))}</span> — {l.where}: {l.text}
                    </li>
                  ))}
                </ul>
              </Disclosure>
            ) : null}
          </>
        ) : (
          <Callout tone="warn" title="The link to the document is missing">
            {ft.note}
          </Callout>
        )}
      </Block>

      <Block title="Where the value is" level={3} testId="fulltext-locator">
        <p className="text-data text-ink" data-testid="fulltext-locator-text">
          {locator ?? <Missing className="!whitespace-normal">No locator</Missing>}
        </p>
        {x ? (
          <Fields
            className="mt-2"
            rows={[
              ["Read", x.target],
              ["Row", <Stored key="r" value={x.row_label} missing="Not a table row" />],
              ["Column", <Stored key="c" value={x.column_header} missing="Not a table column" />],
              ["Compared with", <Stored key="cmp" value={x.comparator_header} missing="No comparator column" />],
            ]}
          />
        ) : null}
        {t?.source_line ? <p className="mt-2 text-caption text-ink-2">Table source line: “{t.source_line}” ({titleCase(t.authorship)})</p> : null}
      </Block>

      {t ? (
        <Block title="Table as extracted" level={3} testId="fulltext-extracted">
          <ExtractedTableView ft={ft} />
          {t.footnotes?.length ? (
            <div className="mt-2" data-testid="fulltext-footnotes">
              <p className="text-caption font-semibold text-ink-2">Footnotes</p>
              <ul className="text-caption text-ink-2">
                {t.footnotes.map((f, i) => (
                  <li key={i}>
                    {f.marker ? <sup className="mr-0.5">{f.marker}</sup> : null}
                    {f.text}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {t.structural_notes?.length ? (
            <Disclosure summary={`Structure notes from the parser (${t.structural_notes.length})`} className="mt-2">
              <ul className="list-disc pl-4 text-caption text-ink-2">
                {t.structural_notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </Disclosure>
          ) : null}
        </Block>
      ) : passage?.sentence ? (
        <Block title="Sentence as extracted" level={3} testId="fulltext-extracted">
          <blockquote className="rounded-control border-l-[3px] border-l-ink-3 bg-sunken px-3 py-2 text-data leading-relaxed text-ink">{String(passage.sentence)}</blockquote>
          {passage.lead ? <p className="mt-1 text-caption text-ink-3">The value is the interval printed after “{String(passage.lead)}”.</p> : null}
        </Block>
      ) : null}

      {x ? (
        <Block title="Parsing method and quality assessment" level={3} testId="fulltext-quality">
          <Fields
            rows={[
              ["Parsing method", <span key="m" className="mono break-all">{`${x.parse_method} (version ${x.parse_version})`}</span>],
              ["Extraction confidence", <span key="c" data-testid="fulltext-confidence">{x.confidence}</span>],
            ]}
          />
          <dl className="mt-2 grid grid-cols-1 gap-y-1.5 text-caption">
            {quality.map(([k, v]) => (
              <div key={k} className="min-w-0">
                <dt className="font-semibold text-ink-2">{titleCase(k.replace(/_/g, " "))}</dt>
                <dd className="min-w-0 break-words text-ink">
                  <QualityValue value={v} />
                </dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-caption text-ink-3">{ft.note}</p>
        </Block>
      ) : null}
    </div>
  );
}
