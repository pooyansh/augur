import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import type { StrategyRollup } from "../lib/api.ts";

interface StrategyPnlChartProps {
  strategies: StrategyRollup[];
}

export function StrategyPnlChart({ strategies }: StrategyPnlChartProps) {
  const data = strategies.map((s) => ({
    name: s.strategy,
    pnl: s.gross_pnl,
  }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: 8, bottom: 4 }}>
        <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#9ca3af" }} />
        <YAxis tick={{ fontSize: 11, fill: "#9ca3af" }} />
        <Tooltip
          contentStyle={{ background: "#1f2937", border: "1px solid #374151" }}
          labelStyle={{ color: "#e5e7eb" }}
          itemStyle={{ color: "#a5b4fc" }}
        />
        <Bar dataKey="pnl" name="Gross PnL">
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.pnl >= 0 ? "#22c55e" : "#ef4444"} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
