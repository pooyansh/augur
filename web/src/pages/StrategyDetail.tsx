import { useQuery } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { api } from "../lib/api.ts";

export default function StrategyDetail() {
  const { name } = useParams<{ name: string }>();

  const q = useQuery({
    queryKey: ["strategy", name],
    queryFn: () => api.strategy(name!),
    enabled: !!name,
    refetchInterval: 60_000,
  });

  if (q.isLoading) return <p className="text-gray-500">Loading...</p>;
  if (q.isError) {
    return (
      <div>
        <p className="text-red-400">Strategy not found.</p>
        <Link to="/strategies" className="text-blue-400 hover:underline text-sm">
          ← Back to strategies
        </Link>
      </div>
    );
  }

  const detail = q.data!;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/strategies" className="text-gray-500 hover:text-gray-300 text-sm">
          ← Strategies
        </Link>
        <h1 className="text-xl font-semibold">{detail.strategy}</h1>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Wins</div>
          <div className="text-green-400 font-bold">{detail.summary.wins}</div>
        </div>
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Losses</div>
          <div className="text-red-400 font-bold">{detail.summary.losses}</div>
        </div>
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Gross PnL</div>
          <div className={detail.summary.gross_pnl >= 0 ? "text-green-400" : "text-red-400"}>
            {detail.summary.gross_pnl.toFixed(2)}
          </div>
        </div>
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Orders</div>
          <div>{detail.summary.n_orders}</div>
        </div>
      </div>

      <div>
        <h2 className="text-sm font-medium text-gray-400 mb-3 uppercase tracking-wide">
          Per-Bot Breakdown
        </h2>
        <table className="w-full text-sm text-left">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-2 pr-4">Bot ID</th>
              <th className="pb-2 pr-4">Market</th>
              <th className="pb-2 pr-4">Wins</th>
              <th className="pb-2 pr-4">Losses</th>
              <th className="pb-2 pr-4">Gross PnL</th>
              <th className="pb-2">Orders</th>
            </tr>
          </thead>
          <tbody>
            {detail.bots.map((b) => (
              <tr key={b.bot_id} className="border-b border-gray-800">
                <td className="py-2 pr-4 font-mono text-xs">
                  <Link
                    to={`/bots/${encodeURIComponent(b.bot_id)}`}
                    className="text-blue-400 hover:underline"
                  >
                    {b.bot_id}
                  </Link>
                </td>
                <td className="py-2 pr-4 font-mono text-xs">{b.market_id}</td>
                <td className="py-2 pr-4 text-green-400">{b.wins}</td>
                <td className="py-2 pr-4 text-red-400">{b.losses}</td>
                <td className={`py-2 pr-4 ${b.gross_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {b.gross_pnl.toFixed(2)}
                </td>
                <td className="py-2">{b.n_orders}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
