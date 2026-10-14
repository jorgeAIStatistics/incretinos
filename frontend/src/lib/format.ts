/**
 * Formatting for interface counts and enum names. Scientific values are NOT formatted here: they go through
 * `fmt` in lib/api.ts, which is owned together with the charts.
 */
const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** A whole-number count with thousands separators. A count that was not provided is an em dash, never 0. */
export const formatCount = (n: number | null | undefined): string => (n === null || n === undefined || Number.isNaN(n) ? "—" : integer.format(n));

/** Share of a total as a whole percentage. Returns null when the total is zero, so the caller cannot print 0% of nothing. */
export const share = (part: number, total: number): number | null => (total > 0 ? (100 * part) / total : null);

const TRIAL_STATUS: Record<string, string> = {
  ACTIVE_NOT_RECRUITING: "Active, not recruiting",
  RECRUITING: "Recruiting",
  COMPLETED: "Completed",
  NOT_YET_RECRUITING: "Not yet recruiting",
  ENROLLING_BY_INVITATION: "Enrolling by invitation",
  TERMINATED: "Terminated",
  WITHDRAWN: "Withdrawn",
  SUSPENDED: "Suspended",
  UNKNOWN: "Status unknown",
};

const sentence = (s: string): string => s.toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

/** Registry recruitment status in plain words. An unknown code is shown in sentence case rather than raw. */
export const trialStatusLabel = (status: string | null | undefined): string => (status ? (TRIAL_STATUS[status] ?? sentence(status)) : "Not reported");

/** Any SCREAMING_SNAKE enum value as a sentence-case phrase. Prefer a label from the API when one exists. */
export const enumLabel = (value: string | null | undefined): string => (value ? sentence(value) : "Not reported");
