import type { ChartRow } from "@/components/charts";
import { fmt, titleCase } from "@/lib/api";

const ESTIMAND: Record<string, string> = { EFFICACY: "Efficacy estimand", TREATMENT_REGIMEN: "Treatment-regimen estimand", NOT_STATED: "Estimand not stated" };
export const estimandLabel = (label: string, basis?: string) => `${ESTIMAND[label] ?? titleCase(label)}${basis?.includes("INFERRED") ? " (inferred)" : ""}`;

/* ---------- units ---------- */

// Spellings of one unit are folded into one symbol. Nothing is converted: a value is never moved from one unit to another.
const UNIT_SYMBOL: [RegExp, string][] = [
  [/^kilograms?$|^kgs?$/i, "kg"],
  [/^centimet(?:er|re)s?$|^cms?$/i, "cm"],
  [/^mm ?hg$/i, "mmHg"],
  [/^mg\/dl$/i, "mg/dL"],
  [/^mg\/l$/i, "mg/L"],
  [/^ng\/ml$/i, "ng/mL"],
  [/^ng\*h(?:r)?\/ml$/i, "ng·h/mL"],
  [/^iu\/day$/i, "IU/day"],
  [/^kg\/m\^?2$/i, "kg/m²"],
  [/^t[- ]?score$/i, "T-score"],
  [/^participants$/i, "participants"],
];

/** The unit as it is printed beside a number ("" when the source gives none). */
export function unitLabel(unit: string | null | undefined): string {
  let u = (unit ?? "").trim();
  if (!u) return "";
  if (u === "%" || /^percent(?:age)? change$/i.test(u)) return "%";
  const share = /^(?:percent(?:age)?|%) of (participants|patients)$/i.exec(u);
  if (share) return `% of ${share[1].toLowerCase()}`;
  if (/^(?:percent(?:age)?|%) (?:of )?hba1c$/i.test(u)) return "% HbA1c";
  const abbreviation = /\(([^()]+)\)\s*$/.exec(u); // "Kilogram (kg)" → "kg"
  if (abbreviation) u = abbreviation[1].trim();
  for (const [pattern, symbol] of UNIT_SYMBOL) if (pattern.test(u)) return symbol;
  return u;
}

/** A number with its unit: "−20.9%", "−2.5 kg", "40% of participants". */
export function withUnit(text: string, unit: string | null | undefined): string {
  const u = unitLabel(unit);
  if (!u) return text;
  return u.startsWith("%") ? `${text}${u}` : `${text} ${u}`;
}

/** Unit of a difference between two values in `unit`: two percentages differ by percentage points. */
export function differenceUnit(unit: string | null | undefined): string {
  const u = unitLabel(unit);
  return u.startsWith("%") ? "percentage points" : u;
}

/* ---------- reported numbers ---------- */

/** The source's own text for a number when the API provides it and it denotes the same number; otherwise the stored value, unrounded. */
export function reportedText(value: number | null | undefined, text?: string | null): string {
  if (typeof text === "string" && value != null) {
    const t = text.trim().replace(/^[-−]/, "-");
    if (/^-?\d+(?:\.\d+)?$/.test(t) && Number(t) === value) return t.replace("-", "−");
  }
  return fmt(value);
}

/**
 * How a confidence interval is introduced: "95% CI", or an explicit statement when the source gives no level.
 * A posted level outside 0–100 is not a confidence level (one registry record posts its estimate in that field);
 * it is shown as posted and named as invalid rather than printed as if it were a level.
 */
export function ciLabel(level: number | null | undefined): string {
  if (level == null) return "CI (level not stated)";
  if (level > 0 && level < 100) return `${fmt(level)}% CI`;
  return `CI (level posted by the source as ${fmt(level)}%, which is not a valid confidence level)`;
}

/* ---------- arm roles ---------- */

export type StudyDrugRole = { slug: string; role: string };
type ArmLike = { is_placebo?: boolean | null; drug_slug?: string | null; arm_type?: string | null; arm_role?: string | null };

/**
 * What an arm is in its trial, from data: the API's own `arm_role` where a row carries one, otherwise the arm's
 * placebo flag, its registered arm type and the role its drug has in the trial. The wording of the arm label is
 * never used. (Same rule as `arm_role` in the backend safety engine.)
 */
export function armGroup(arm: ArmLike, drugs?: StudyDrugRole[] | null): ChartRow["group"] {
  if (arm.arm_role) return arm.arm_role === "PLACEBO" ? "control" : arm.arm_role === "ACTIVE_COMPARATOR" ? "comparator" : arm.arm_role === "STUDY_DRUG" ? "active" : "other";
  if (arm.is_placebo) return "control";
  if (arm.arm_type === "PLACEBO_COMPARATOR") return "other"; // registered as placebo but stored with an active treatment: not a pure placebo arm
  if (arm.arm_type === "ACTIVE_COMPARATOR") return "comparator";
  const components = String(arm.drug_slug ?? "").split("+").filter(Boolean);
  if (!drugs || !components.length) return "other";
  const roles = components.map((slug) => drugs.find((d) => d.slug === slug)?.role);
  if (roles.some((r) => r === "INVESTIGATIONAL")) return "active";
  if (roles.every((r) => r === "COMPARATOR")) return "comparator";
  return "other";
}

/** Placebo first, then active comparators, then study-drug arms by dose; ties keep the order the API returned. */
export function byRoleThenDose<T extends { group: ChartRow["group"] }>(rows: T[], dose: (row: T) => number | null | undefined): T[] {
  const rank = { control: 0, comparator: 1, active: 2, other: 3 } as const;
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => rank[a.row.group] - rank[b.row.group] || (dose(a.row) ?? Infinity) - (dose(b.row) ?? Infinity) || a.index - b.index)
    .map((x) => x.row);
}

/* ---------- a finding as a chart row ---------- */

export const weekLabel = (weeks: number | null | undefined): string => (weeks != null ? `week ${fmt(weeks)}` : "time point not stated");
export const tierLabel = (f: any): string => f.source?.tier_label ?? titleCase(f.tier);
export const sourceShort = (source: any): string => (source ? [source.publisher ?? source.title, source.published_date].filter(Boolean).join(", ") : "not recorded");

/** Estimand · evidence tier · time point (· baseline) (· the sub-measure, where an endpoint reports several, e.g. a responder threshold). */
const basisLine = (f: any): string =>
  [estimandLabel(f.estimand_label, f.estimand_basis), tierLabel(f), weekLabel(f.timepoint_weeks), f.baseline_definition ? `from ${f.baseline_definition}` : null, f.class_title || null].filter(Boolean).join(" · ");

/** The lines every plotted result carries: estimand, evidence tier, time point (and baseline), then its source. */
export function findingMeta(f: any): string[] {
  return [basisLine(f), `Source: ${sourceShort(f.source)}`];
}

/** A finding becomes a chart row without losing its unit, its interval basis or its provenance. */
export function findingRow(f: any, opts: { studyInLabel?: boolean; drugs?: StudyDrugRole[] | null; arms?: any[] | null; keyPrefix?: string } = {}): ChartRow {
  const reported = f.ci_low != null && f.ci_high != null;
  const derived = !reported && f.derived_interval ? f.derived_interval : null;
  const derivedLevel = derived?.ci_level != null ? (derived.ci_level <= 1 ? derived.ci_level * 100 : derived.ci_level) : null;
  const armType = f.arm_type ?? opts.arms?.find((a: any) => a.id === f.arm_id)?.arm_type ?? null;
  const unit = unitLabel(f.unit);
  const interval = reported
    ? `${ciLabel(f.ci_level)} ${fmt(f.ci_low)} to ${withUnit(fmt(f.ci_high), unit)}, reported by the source`
    : derived
      ? `${f.dispersion_type ?? "SE"} ${withUnit(fmt(f.dispersion_value), unit)} reported; ${fmt(derivedLevel ?? 95)}% interval ${fmt(derived.ci_low, 2)} to ${withUnit(fmt(derived.ci_high, 2), unit)} derived here (not reported by the source)`
      : "No interval reported by the source";
  return {
    key: `${opts.keyPrefix ?? ""}${f.result_id}`,
    label: opts.studyInLabel ? `${f.study_name} · ${f.arm_label}` : f.arm_label,
    meta: findingMeta(f),
    value: f.value,
    valueText: f.value != null ? reportedText(f.value, f.value_text) : undefined,
    unit,
    lo: reported ? f.ci_low : derived ? derived.ci_low : null,
    hi: reported ? f.ci_high : derived ? derived.ci_high : null,
    intervalKind: reported ? "reported" : derived ? "derived" : undefined,
    intervalLevel: reported ? (f.ci_level ?? null) : derived ? (derivedLevel ?? 95) : null,
    range: f.value == null && f.value_low != null && f.value_high != null ? [f.value_low, f.value_high] : null,
    group: armGroup({ ...f, arm_type: armType }, opts.drugs),
    tip: [f.n_analyzed != null ? `n = ${f.n_analyzed}` : "n not reported", interval, `${tierLabel(f)} · ${titleCase(f.verification_status)}`],
    href: f.result_id != null ? `/compare?a=${f.result_id}` : undefined,
  };
}

/* ---------- coherent result sets ---------- */

export type FindingSet = { key: string; findings: any[]; first: any; unit: string };

/**
 * Results may share an axis and a caption only when they share endpoint (and sub-measure, such as a responder
 * threshold), estimand, evidence tier, source, time point, baseline definition and unit. This partitions findings
 * into such sets, in the order given.
 */
export function findingSets(findings: any[]): FindingSet[] {
  const sets = new Map<string, FindingSet>();
  for (const f of findings) {
    const unit = unitLabel(f.unit);
    const key = [f.endpoint_id ?? f.endpoint_title ?? "", f.class_title ?? "", f.estimand_label, f.estimand_basis, f.tier, f.source?.id ?? "no-source", f.timepoint_weeks ?? "no-time", f.baseline_definition ?? "", unit].join("¦");
    const set = sets.get(key);
    if (set) set.findings.push(f);
    else sets.set(key, { key, findings: [f], first: f, unit });
  }
  return [...sets.values()];
}

/** What a set is, in the order estimand · evidence tier · time point (· baseline) (· sub-measure). True of every row in the set. */
export function setLabel(set: FindingSet): string {
  return basisLine(set.first);
}

/* ---------- between-arm contrasts ---------- */

export const isRatio = (paramType: string | null | undefined): boolean => /ratio/i.test(paramType ?? "");

/**
 * What is subtracted from (or divided by) what in a reported between-arm analysis, and how that direction is known.
 * `basis` is one of a small set of sentences, so a table can print each one once as a footnote keyed by `mark`.
 */
export function contrastDirection(a: any): { expression: string; mark: string; basis: string } {
  const treatment = a.treatment_label ?? "treatment not stated";
  if (!a.comparator_label) return { expression: `${treatment}; comparator not stated`, mark: "‡", basis: "The source does not name the comparator, so the direction of this contrast is not on record." };
  const expression = isRatio(a.param_type) ? `${treatment} ÷ ${a.comparator_label}` : `${treatment} − ${a.comparator_label}`;
  if (a.origin === "CURATED") return { expression, mark: "a", basis: "Direction as curated from the cited source (treatment relative to comparator); the quoted source text is the authority." };
  if (/does not state which group is the reference/i.test(a.comment ?? "")) return { expression, mark: "c", basis: "The registry does not say which group is the reference: the groups are in registry order and the sign may be the reverse." };
  return { expression, mark: "b", basis: "Reference arm identified by its role in the trial (placebo or active comparator); the registry posts the estimate without stating the direction." };
}

/** The unit printed beside a between-arm estimate: a ratio has none, a difference of percentages is in percentage points. */
export function contrastUnit(a: any): string {
  return isRatio(a.param_type) ? "" : differenceUnit(a.unit);
}
