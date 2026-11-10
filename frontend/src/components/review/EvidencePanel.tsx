"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { Callout, Disclosure, Missing, Term, VerificationBadge } from "@/components/ui";
import { diabetesLabel, phaseLabel, titleCase } from "@/lib/api";
import { trialStatusLabel } from "@/lib/format";
import type { GlossaryKey } from "@/lib/glossary";
import { type EvidenceDetail, evidenceTypeLabel, fieldLabel, recordHref, type RelatedRow } from "@/lib/review";
import { estimandLabel } from "@/lib/rows";
import { Block, Fields, Stored } from "./bits";

/*
 * CENTER panel: the structured evidence exactly as stored. Every value is printed with its stored digits; a value the
 * source did not report is written out as missing. Nothing here is recalculated or rounded.
 */

const ROLE_LABEL: Record<string, string> = { arm: "Arm", treatment: "Treatment arm", comparator: "Comparator arm" };
const STRATEGY_TERM: Record<string, GlossaryKey> = { TREATMENT_POLICY: "treatment-policy-strategy", HYPOTHETICAL: "hypothetical-strategy" };
const ESTIMAND_TERM: Record<string, GlossaryKey> = { TREATMENT_REGIMEN: "treatment-regimen-estimand", EFFICACY: "efficacy-estimand" };

/** The figure a reviewer checks first, per evidence type. Falls back to the value summary of the stored row. */
function Headline({ detail }: { detail: EvidenceDetail }) {
  const v = detail.values ?? {};
  const unit = typeof v.unit === "string" && v.unit ? v.unit : null;
  const ci = detail.confidence_interval ?? null;
  const ciText =
    ci && ci.status === "REPORTED" ? (
      <span className="num">
        {ci.level != null ? `${ci.level}% ` : ""}
        <Term k="confidence-interval">CI</Term> <Stored value={ci.low} /> to <Stored value={ci.high} />
        {ci.sides ? ` (${ci.sides.toLowerCase()})` : ""}
      </span>
    ) : (
      <Missing className="!whitespace-normal">Confidence interval not reported</Missing>
    );

  let main: ReactNode = null;
  let extra: [string, ReactNode][] = [];
  switch (detail.evidence_type) {
    case "result":
      main = <Stored value={v.value_text ?? v.value} />;
      extra = [
        ["Interval", ciText],
        ["Range reported", v.value_low != null || v.value_high != null ? <span key="r"><Stored value={v.value_low} /> to <Stored value={v.value_high} /></span> : <Missing key="r" className="!whitespace-normal">Not reported</Missing>],
        ["Dispersion", v.dispersion_type ? <span key="d">{String(v.dispersion_type)} <Stored value={v.dispersion_value} /></span> : <Missing key="d" className="!whitespace-normal">Not reported</Missing>],
        ["n analysed", <Stored key="n" value={v.n_analyzed} />],
        ["Parameter", <Stored key="p" value={v.param_type} />],
      ];
      break;
    case "analysis":
      main = <Stored value={v.estimate_text ?? v.estimate} />;
      extra = [
        ["Interval", ciText],
        ["p value", <Stored key="p" value={v.p_value_text} />],
        ["Parameter", <Stored key="t" value={v.param_type} />],
        ["Method", <Stored key="m" value={v.method} />],
        ["Non-inferiority margin", v.ni_margin_text || v.ni_margin != null ? <Stored key="ni" value={v.ni_margin_text ?? v.ni_margin} /> : <Missing key="ni" className="!whitespace-normal">Not reported</Missing>],
      ];
      break;
    case "adverse_event":
      main = v.n_affected != null || v.n_at_risk != null ? (
        <span>
          <Stored value={v.n_affected} /> <span className="text-body font-normal text-ink-2">of</span> <Stored value={v.n_at_risk} />
        </span>
      ) : v.percent != null ? (
        <Stored value={v.percent} />
      ) : null;
      extra = [
        ["Percent", v.percent != null ? <span key="pc"><Stored value={v.percent} />%</span> : <Missing key="pc" className="!whitespace-normal">Not reported</Missing>],
        ["Events", <Stored key="e" value={v.n_events} />],
        ["Term", <Stored key="t" value={v.term} />],
        ["Serious", <Stored key="s" value={v.is_serious} />],
        ["Period", <Stored key="pe" value={v.period} />],
      ];
      break;
    default:
      main = null;
  }

  const statement = typeof v.statement === "string" ? v.statement : typeof v.text === "string" ? v.text : null;
  return (
    <div data-testid="evidence-headline">
      {main !== null ? (
        <p className="num text-display font-semibold leading-none text-ink" data-testid="stored-value">
          {main}
          {unit ? <span className="ml-1.5 text-body font-normal text-ink-2">{unit}</span> : null}
        </p>
      ) : statement ? (
        <p className="text-body leading-relaxed text-ink" data-testid="stored-value">
          {statement}
        </p>
      ) : detail.evidence_type === "mechanism" ? (
        <p className="text-body text-ink" data-testid="stored-value">
          <Stored value={v.target} /> — <Stored value={v.action ? titleCase(String(v.action)) : null} />
        </p>
      ) : (
        <Missing className="!whitespace-normal">No headline value is stored for this record</Missing>
      )}
      {extra.length ? <Fields className="mt-3" rows={extra} /> : null}
    </div>
  );
}

function RelatedList({ rows, empty }: { rows: RelatedRow[]; empty: string }) {
  if (!rows.length) return <p className="text-data text-ink-3">{empty}</p>;
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((r) => (
        <li key={`${r.evidence_type}-${r.id}`} className="rounded-control border border-hairline px-2.5 py-2 text-data">
          <div className="flex flex-wrap items-center gap-1.5">
            <VerificationBadge status={r.verification_status} compact />
            {r.relation ? <span className="text-caption text-ink-3">{r.relation}</span> : null}
          </div>
          <p className="mt-1 text-ink">
            {r.evidence_key ? (
              <Link href={recordHref(r.evidence_type, r.evidence_key)} className="text-accent hover:underline">
                {r.label}
              </Link>
            ) : (
              r.label
            )}
          </p>
          {r.values && Object.keys(r.values).length ? (
            <p className="num mt-0.5 text-caption text-ink-2">
              {Object.entries(r.values)
                .filter(([, x]) => x !== null && x !== undefined && typeof x !== "object")
                .map(([k, x]) => `${fieldLabel(k)} ${String(x).replace(/^-(?=\d)/, "−")}`)
                .join(" · ")}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function EvidencePanel({ detail }: { detail: EvidenceDetail }) {
  const study = detail.study ?? null;
  const pop = detail.population ?? null;
  const endpoint = detail.endpoint ?? null;
  const tp = detail.time_point ?? null;
  const estimand = detail.estimand ?? null;
  const arms = detail.arms ?? [];
  const drugs = detail.drugs ?? [];
  const formulations = detail.formulations ?? [];
  const missing = detail.missing_fields ?? [];
  const values = Object.entries(detail.values ?? {});

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="evidence-panel">
      <Block title="Stored value" level={3}>
        <Headline detail={detail} />
      </Block>

      <Block title="Missing information" level={3} testId="missing-information">
        {missing.length ? (
          <ul className="flex flex-col gap-1">
            {missing.map((m) => (
              <li key={m}>
                <Missing className="!whitespace-normal">
                  <span>
                    <span className="font-medium text-ink-2">{fieldLabel(m)}</span>: not reported by the source or not captured
                  </span>
                </Missing>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-data text-ink-3">The service lists no missing fields for this record.</p>
        )}
      </Block>

      <Block title="Drug and formulation" level={3}>
        {drugs.length ? (
          <ul className="flex flex-col gap-1 text-data">
            {drugs.map((d) => (
              <li key={d.slug}>
                <span className="font-medium text-ink">{d.name}</span>
                {d.code_name ? <span className="text-ink-3"> ({d.code_name})</span> : null}
                <span className="text-ink-2">{[d.regulatory_status ? titleCase(d.regulatory_status) : null, d.route, d.sponsor].filter(Boolean).map((x) => ` · ${x}`)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <Missing className="!whitespace-normal">No drug is linked</Missing>
        )}
        {formulations.length ? (
          <ul className="mt-1.5 flex flex-col gap-1 text-data">
            {formulations.map((f) => (
              <li key={f.key} className="text-ink-2">
                Formulation: <span className="text-ink">{f.name}</span>
                {[f.dosage_form, f.route, f.stage ? titleCase(f.stage) : null].filter(Boolean).map((x) => ` · ${x}`)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-caption text-ink-3">No formulation is linked.</p>
        )}
      </Block>

      <Block title="Trial and population" level={3}>
        {study ? (
          <Fields
            rows={[
              [
                "Trial",
                <span key="t">
                  <Link href={`/trials/${study.nct_id}`} className="font-medium text-accent hover:underline">
                    {study.display_name}
                  </Link>
                  <span className="mono block text-caption text-ink-3">{study.nct_id}</span>
                </span>,
              ],
              ["Design", [phaseLabel(study.phase), study.allocation ? titleCase(study.allocation) : null, study.masking ? `${titleCase(study.masking)} masking` : null].filter(Boolean).join(" · ") || <Missing key="d" className="!whitespace-normal">Not reported</Missing>],
              ["Recruitment status", trialStatusLabel(study.overall_status)],
              ["Population", pop ? diabetesLabel(pop.diabetes_status) : <Missing key="p" className="!whitespace-normal">Not reported</Missing>],
              ...(pop?.diabetes_basis ? ([["Basis for the population", pop.diabetes_basis]] as [string, ReactNode][]) : []),
              ...(pop ? ([["Sex and age", [pop.sex ? titleCase(pop.sex) : null, pop.min_age ? `from ${pop.min_age}` : null, pop.max_age ? `to ${pop.max_age}` : null].filter(Boolean).join(", ") || "Not reported"]] as [string, ReactNode][]) : []),
            ]}
          />
        ) : (
          <Missing className="!whitespace-normal">No trial is linked to this record</Missing>
        )}
        {study?.brief_title ? <p className="mt-2 text-caption text-ink-3">{study.brief_title}</p> : null}
      </Block>

      <Block title={arms.length > 1 ? "Arms" : "Arm"} level={3} testId="arms">
        {arms.length ? (
          <ul className="flex flex-col gap-2">
            {arms.map((a, i) => (
              <li key={`${a.role}-${i}`} className="rounded-control border border-hairline px-2.5 py-2 text-data">
                <p className="text-caption font-medium text-ink-3">{ROLE_LABEL[a.role] ?? titleCase(a.role)}</p>
                <p className="font-medium text-ink">{a.label ?? <Missing className="!whitespace-normal">No label</Missing>}</p>
                {a.registered_label && a.registered_label !== a.label ? <p className="text-caption text-ink-2">Registered as: {a.registered_label}</p> : null}
                <p className="mt-0.5 text-caption text-ink-2">
                  {[
                    a.drug_slug ? `Drug ${a.drug_slug}` : null,
                    a.dose_amount != null ? `dose ${a.dose_amount} ${a.dose_unit ?? ""}`.trim() : a.dose_text ? `dose ${a.dose_text}` : "dose not stated",
                    a.formulation_key ? `formulation ${a.formulation_key}` : null,
                    a.is_placebo ? "placebo" : null,
                    a.is_pooled ? "pooled arms" : null,
                    a.n_randomized != null ? `${a.n_randomized} randomized` : null,
                    a.arm_type ? titleCase(a.arm_type) : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
                {a.linked === false ? <p className="mt-1 text-caption font-medium text-warn">Not linked to a registered arm of the trial.</p> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-data text-ink-3">This kind of record has no arm.</p>
        )}
      </Block>

      <Block title="Endpoint" level={3} testId="endpoint">
        {endpoint ? (
          <>
            <p className="text-data font-medium text-ink">{endpoint.title}</p>
            <Fields
              className="mt-2"
              rows={[
                ["Category", endpoint.category_label ?? (endpoint.category ? titleCase(endpoint.category) : <Missing key="c" className="!whitespace-normal">Not classified</Missing>)],
                ["Role", endpoint.role ? titleCase(endpoint.role) : <Missing key="r" className="!whitespace-normal">Not stated</Missing>],
                ["Threshold", <Stored key="th" value={endpoint.threshold} missing="None" />],
                ["Unit", <Stored key="u" value={endpoint.unit} />],
                ["Time frame (as registered)", <Stored key="tf" value={endpoint.timeframe_text} />],
                ["Hierarchy rank", <Stored key="h" value={endpoint.hierarchy_rank} missing="Not stated" />],
                [<Term key="mt" k="multiplicity-control">Multiplicity control</Term>, endpoint.multiplicity_controlled ? titleCase(endpoint.multiplicity_controlled) : <Missing key="m" className="!whitespace-normal">Not stated</Missing>],
                ...(endpoint.is_pooled_arms ? ([["Arms", "Pooled arms"]] as [string, ReactNode][]) : []),
              ]}
            />
            {endpoint.description ? (
              <Disclosure summary="Endpoint description" className="mt-2">
                <p className="whitespace-pre-line text-caption text-ink-2">{endpoint.description}</p>
              </Disclosure>
            ) : null}
          </>
        ) : (
          <p className="text-data text-ink-3">This kind of record has no endpoint.</p>
        )}
      </Block>

      <Block title="Time point" level={3}>
        {tp && (tp.weeks != null || tp.text || tp.baseline_definition) ? (
          <Fields
            rows={[
              ["Weeks", <Stored key="w" value={tp.weeks} />],
              ["As written", <Stored key="t" value={tp.text} />],
              ["Baseline", <Stored key="b" value={tp.baseline_definition} />],
            ]}
          />
        ) : (
          <Missing className="!whitespace-normal">Time point not stated</Missing>
        )}
      </Block>

      <Block title="Estimand" level={3} testId="estimand">
        {estimand ? (
          <>
            <p className="text-data text-ink">
              <Term k="estimand">Estimand</Term>:{" "}
              {ESTIMAND_TERM[estimand.label] ? <Term k={ESTIMAND_TERM[estimand.label]}>{estimandLabel(estimand.label)}</Term> : <span className="font-medium">{estimandLabel(estimand.label)}</span>}
            </p>
            <p className="mt-0.5 text-caption text-ink-2">How it was established: {estimand.basis ? titleCase(estimand.basis) : "not stated"}</p>
            {estimand.description ? <p className="mt-1.5 text-data text-ink-2">{estimand.description}</p> : null}
            {estimand.population_text ? <p className="mt-1 text-caption text-ink-2">Population: {estimand.population_text}</p> : null}
            {estimand.quote ? <blockquote className="mt-1.5 border-l-2 border-l-rule pl-2 text-caption italic text-ink-2">“{estimand.quote}”</blockquote> : null}
            {estimand.strategies?.length ? (
              <ul className="mt-2 flex flex-col gap-1 text-data">
                {estimand.strategies.map((s, i) => (
                  <li key={i} className="text-ink-2">
                    <Term k="intercurrent-event">{titleCase(s.event)}</Term>:{" "}
                    {STRATEGY_TERM[s.strategy] ? <Term k={STRATEGY_TERM[s.strategy]}>{titleCase(s.strategy)}</Term> : titleCase(s.strategy)}
                    {s.basis ? <span className="text-caption text-ink-3"> ({titleCase(s.basis)})</span> : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : (
          <p className="text-data text-ink-3">This kind of record has no estimand.</p>
        )}
      </Block>

      <Block title="Related analyses" level={3}>
        <RelatedList rows={detail.related_analyses ?? []} empty="No related statistical analysis is stored." />
      </Block>

      <Block title="Every stored field" level={3}>
        <Disclosure summary={`Show all ${values.length} stored fields of this ${evidenceTypeLabel(detail.evidence_type).toLowerCase()}`}>
          <dl className="grid grid-cols-1 gap-y-1 text-caption">
            {values.map(([k, v]) => (
              <div key={k} className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-x-3 border-b border-hairline py-1 last:border-b-0">
                <dt className="mono break-all text-ink-3">{k}</dt>
                <dd className="min-w-0 break-words text-ink">
                  <Stored value={v} />
                </dd>
              </div>
            ))}
          </dl>
        </Disclosure>
        {detail.identity_collision ? (
          <Callout tone="warn" title="Identity collision" className="mt-2">
            {detail.identity_collision.explanation ?? `Another stored row shares this record's identity (level ${detail.identity_collision.level}).`}
          </Callout>
        ) : null}
      </Block>
    </div>
  );
}
