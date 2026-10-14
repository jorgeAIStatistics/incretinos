"use client";

import { ShieldAlert } from "lucide-react";
import { Async, Badge, type Column, DataTable, TableSkeleton } from "@/components/ui";
import { useApi } from "@/lib/api";
import { formatCount } from "@/lib/format";
import type { ValidationReport, ValidationRule } from "@/lib/types";

const COLUMNS: Column<ValidationRule>[] = [
  { key: "rule", header: "Rule", rowHeader: true, className: "mono whitespace-nowrap", cell: (r) => <span translate="no">{r.rule_id}</span> },
  {
    key: "guarantee",
    header: "What it guarantees",
    className: "min-w-[320px]",
    cell: (r) => (
      <>
        {r.title}
        {r.detail ? <span className="block text-caption text-ink-3">{r.detail}</span> : null}
        {r.violations.map((v) => (
          <span key={v} className="mt-1 flex items-start gap-1.5 text-caption text-critical">
            <ShieldAlert aria-hidden size={13} className="mt-px shrink-0" />
            <span>Violation: {v}</span>
          </span>
        ))}
      </>
    ),
  },
  { key: "checked", header: "Items checked", align: "right", cell: (r) => formatCount(r.checked) },
  { key: "result", header: "Result", cell: (r) => <Badge tone={r.passed ? "ok" : "critical"}>{r.passed ? "Pass" : "Fail"}</Badge> },
];

export function ValidationTab() {
  const state = useApi<ValidationReport>("/validation");
  return (
    <Async state={state} errorTitle="The validation rules could not be loaded" loading={<TableSkeleton rows={8} columns={4} label="Running validation rules…" />}>
      {(report) => (
        <>
          <p className="mb-3 max-w-[80ch] text-data text-ink-2">
            <span className="num font-semibold text-ink">
              {report.passed} of {report.total}
            </span>{" "}
            scientific validation rules pass on the current evidence base. Each rule is also exercised with deliberately corrupted evidence in the test suite.
          </p>
          <DataTable
            label="Scientific validation rules"
            columns={COLUMNS}
            // Failing rules are listed first so a failure is never below the fold.
            rows={[...report.rules.filter((r) => !r.passed), ...report.rules.filter((r) => r.passed)]}
            rowKey={(r) => r.rule_id}
            maxHeight="none"
            minWidth={680}
            footer={<p className="num text-data text-ink-2">{report.rules.length === report.total ? `All ${formatCount(report.total)} rules are listed.` : `${formatCount(report.rules.length)} rules listed; the service reports ${formatCount(report.total)}.`}</p>}
          />
        </>
      )}
    </Async>
  );
}
