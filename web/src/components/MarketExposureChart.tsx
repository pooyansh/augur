import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { MarketExposure } from "../lib/api.ts";

interface MarketExposureChartProps {
  markets: MarketExposure[];
}

export function MarketExposureChart({ markets }: MarketExposureChartProps) {
  const data = markets.map((m) => ({
    name: m.market_id.slice(0, 16),
    pnl: m.gross_pnl,
    bots: m.n_bots,
  }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: 8, bottom: 4 }}>
        <XAxis dataKey="name" tick={{ fontSize: 10, fill: "#9ca3af" }} />
        <YAxis tick={{ fontSize: 11, fill: "#9ca3af" }} />
        <Tooltip
          contentStyle={{ background: "#1f2937", border: "1px solid #374151" }}
          labelStyle={{ color: "#e5e7eb" }}
        />
        <Bar dataKey="pnl" fill="#6366f1" name="Gross PnL" />
      </BarChart>
    </ResponsiveContainer>
  );
}
