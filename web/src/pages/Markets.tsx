import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api.ts";
import { useVisibleInterval } from "../lib/refresh.ts";
import { MarketExposureChart } from "../components/MarketExposureChart.tsx";

export default function Markets() {
  const q = useQuery({
    queryKey: ["markets"],
    queryFn: () => api.markets(),
    refetchInterval: false,
  });

  useVisibleInterval(() => void q.refetch(), 60_000);

  const markets = q.data?.markets ?? [];

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Markets</h1>

      {markets.length > 0 && (
        <div>
          <h2 className="text-sm font-medium text-gray-400 mb-2 uppercase tracking-wide">
            Gross PnL by Market
          </h2>
          <MarketExposureChart markets={markets} />
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-2 pr-4">Market ID</th>
              <th className="pb-2 pr-4">Gross PnL</th>
              <th className="pb-2 pr-4">Realized</th>
              <th className="pb-2 pr-4">Unrealized</th>
              <th className="pb-2 pr-4">Bots</th>
              <th className="pb-2">Orders</th>
            </tr>
          </thead>
          <tbody>
            {markets.map((m) => (
              <tr key={m.market_id} className="border-b border-gray-800 hover:bg-gray-800/40">
                <td className="py-2 pr-4 font-mono text-xs">{m.market_id}</td>
                <td
                  className={`py-2 pr-4 ${m.gross_pnl >= 0 ? "text-green-400" : "text-red-400"}`}
                >
                  {m.gross_pnl.toFixed(2)}
                </td>
                <td className="py-2 pr-4">{m.realized_pnl.toFixed(2)}</td>
                <td className="py-2 pr-4">{m.unrealized_pnl.toFixed(2)}</td>
                <td className="py-2 pr-4">{m.n_bots}</td>
                <td className="py-2">{m.n_orders}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
