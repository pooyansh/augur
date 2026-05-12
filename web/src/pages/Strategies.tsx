import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../lib/api.ts";
import { useVisibleInterval } from "../lib/refresh.ts";
import { StrategyPnlChart } from "../components/StrategyPnlChart.tsx";

export default function Strategies() {
  const q = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.strategies(),
    refetchInterval: false,
  });

  useVisibleInterval(() => void q.refetch(), 60_000);

  const strategies = q.data?.strategies ?? [];

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Strategies</h1>

      {strategies.length > 0 && (
        <div>
          <h2 className="text-sm font-medium text-gray-400 mb-2 uppercase tracking-wide">
            Gross PnL by Strategy
          </h2>
          <StrategyPnlChart strategies={strategies} />
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-2 pr-4">Strategy</th>
              <th className="pb-2 pr-4">Wins</th>
              <th className="pb-2 pr-4">Losses</th>
              <th className="pb-2 pr-4">Gross PnL</th>
              <th className="pb-2 pr-4">Orders</th>
              <th className="pb-2 pr-4">Bots</th>
              <th className="pb-2">Markets</th>
            </tr>
          </thead>
          <tbody>
            {strategies.map((s) => (
              <tr key={s.strategy} className="border-b border-gray-800 hover:bg-gray-800/40">
                <td className="py-2 pr-4">
                  <Link
                    to={`/strategies/${encodeURIComponent(s.strategy)}`}
                    className="text-blue-400 hover:underline"
                  >
                    {s.strategy}
                  </Link>
                </td>
                <td className="py-2 pr-4 text-green-400">{s.wins}</td>
                <td className="py-2 pr-4 text-red-400">{s.losses}</td>
                <td className={`py-2 pr-4 ${s.gross_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {s.gross_pnl.toFixed(2)}
                </td>
                <td className="py-2 pr-4">{s.n_orders}</td>
                <td className="py-2 pr-4">{s.n_bots}</td>
                <td className="py-2">{s.n_markets}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
