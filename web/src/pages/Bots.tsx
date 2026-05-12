import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../lib/api.ts";
import { useVisibleInterval } from "../lib/refresh.ts";

export default function Bots() {
  const botsQ = useQuery({
    queryKey: ["bots"],
    queryFn: () => api.bots(),
    refetchInterval: false,
  });

  useVisibleInterval(() => void botsQ.refetch(), 60_000);

  const bots = botsQ.data ?? [];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Bots</h1>
      {bots.length === 0 && !botsQ.isLoading && (
        <p className="text-gray-500 text-sm">No snapshots found.</p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-2 pr-4">Bot ID</th>
              <th className="pb-2 pr-4">Strategy</th>
              <th className="pb-2 pr-4">Market</th>
              <th className="pb-2 pr-4">Mode</th>
              <th className="pb-2 pr-4">Version</th>
              <th className="pb-2">Snapshot At</th>
            </tr>
          </thead>
          <tbody>
            {bots.map((bot) => (
              <tr key={bot.bot_id} className="border-b border-gray-800 hover:bg-gray-800/40">
                <td className="py-2 pr-4">
                  <Link
                    to={`/bots/${encodeURIComponent(bot.bot_id)}`}
                    className="text-blue-400 hover:underline font-mono text-xs"
                  >
                    {bot.bot_id}
                  </Link>
                </td>
                <td className="py-2 pr-4">{bot.strategy}</td>
                <td className="py-2 pr-4 font-mono text-xs">{bot.market_id}</td>
                <td className="py-2 pr-4">
                  <span
                    className={`px-2 py-0.5 rounded text-xs ${
                      bot.mode === "live"
                        ? "bg-amber-900 text-amber-200"
                        : "bg-indigo-900 text-indigo-200"
                    }`}
                  >
                    {bot.mode}
                  </span>
                </td>
                <td className="py-2 pr-4">{bot.version}</td>
                <td className="py-2 text-xs text-gray-400">
                  {new Date(bot.snapshot_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
