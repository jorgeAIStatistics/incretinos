import { useId } from "react";

type Node = { id: string; label: string; detail: string };
type Edge = { from: string; to: string; trial: string | null; nct_id: string | null; kind: string };
export type Network = { nodes: Node[]; edges: Edge[]; text: string };

const POS: Record<string, { x: number; y: number }> = { A: { x: 110, y: 52 }, B: { x: 530, y: 52 }, C: { x: 320, y: 168 } };

const short = (s: string, n = 26) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/**
 * A — C — B: two randomized contrasts (solid lines, each labelled with its trial) and the inferred A–B link (dashed,
 * labelled as not randomized). The drawing is decorative for assistive technology; the same content is given as text.
 */
export function NetworkDiagram({ network }: { network: Network }) {
  const textId = useId();
  const node = (id: string) => network.nodes.find((n) => n.id === id);
  return (
    <figure className="m-0" data-testid="indirect-network">
      <svg viewBox="0 0 640 210" role="img" aria-labelledby={textId} className="h-auto w-full max-w-[640px] text-ink">
        {network.edges.map((e) => {
          const a = POS[e.from], b = POS[e.to];
          const indirect = e.kind !== "RANDOMIZED_WITHIN_TRIAL";
          const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
          return (
            <g key={`${e.from}-${e.to}`}>
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={indirect ? "var(--border-control)" : "var(--accent)"} strokeWidth={indirect ? 1.5 : 2.5} strokeDasharray={indirect ? "6 5" : undefined} />
              <text x={mx} y={indirect ? my - 10 : my - 8} dx={indirect ? 0 : e.from === "A" ? -14 : 14} textAnchor={indirect ? "middle" : e.from === "A" ? "end" : "start"} fontSize="12" fill="currentColor">
                {indirect ? "indirect — never randomized" : (
                  <>
                    <tspan x={mx} dx={e.from === "A" ? -14 : 14}>{short(e.trial ?? "", 22)}</tspan>
                    <tspan x={mx} dx={e.from === "A" ? -14 : 14} dy={14}>{e.nct_id} · randomized</tspan>
                  </>
                )}
              </text>
            </g>
          );
        })}
        {(["A", "C", "B"] as const).map((id) => {
          const n = node(id);
          const p = POS[id];
          return (
            <g key={id}>
              <circle cx={p.x} cy={p.y} r={20} fill="var(--surface)" stroke={id === "C" ? "var(--ink)" : "var(--accent)"} strokeWidth={2} />
              <text x={p.x} y={p.y + 4.5} textAnchor="middle" fontSize="14" fontWeight={600} fill="currentColor">{id}</text>
              <text x={p.x} y={id === "C" ? p.y + 36 : p.y - 28} textAnchor="middle" fontSize="12" fill="currentColor">{short(n?.label ?? id)}</text>
            </g>
          );
        })}
      </svg>
      <figcaption id={textId} className="mt-1 text-data text-ink-2" data-testid="indirect-network-text">
        Network: {network.text}
      </figcaption>
    </figure>
  );
}
