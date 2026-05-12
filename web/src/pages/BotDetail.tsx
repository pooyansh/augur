import { useQuery } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { api } from "../lib/api.ts";

export default function BotDetail() {
  const { botId } = useParams<{ botId: string }>();

  const botQ = useQuery({
    queryKey: ["bot", botId],
    queryFn: () => api.bot(botId!),
    enabled: !!botId,
    refetchInterval: 60_000,
  });

  if (botQ.isLoading) return <p className="text-gray-500">Loading...</p>;
  if (botQ.isError) {
    return (
      <div>
        <p className="text-red-400">
          {botQ.error instanceof Error ? botQ.error.message : "Error loading bot."}
        </p>
        <Link to="/bots" className="text-blue-400 hover:underline text-sm">
          ← Back to bots
        </Link>
      </div>
    );
  }

  const bot = botQ.data!;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/bots" className="text-gray-500 hover:text-gray-300 text-sm">
          ← Bots
        </Link>
        <h1 className="text-xl font-semibold font-mono">{bot.bot_id}</h1>
        <span
          className={`px-2 py-0.5 rounded text-xs ${
            bot.mode === "live"
              ? "bg-amber-900 text-amber-200"
              : "bg-indigo-900 text-indigo-200"
          }`}
        >
          {bot.mode}
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Strategy</div>
          <div>{bot.strategy}</div>
        </div>
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Market</div>
          <div className="font-mono text-xs">{bot.market_id}</div>
        </div>
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Snapshot Version</div>
          <div>{bot.version}</div>
        </div>
        <div className="bg-gray-800 rounded p-3">
          <div className="text-gray-400 text-xs">Snapshot At</div>
          <div>{new Date(bot.snapshot_at).toLocaleString()}</div>
        </div>
      </div>

      <div>
        <h2 className="text-sm font-medium text-gray-400 mb-2 uppercase tracking-wide">
          State Snapshot
        </h2>
        <pre className="bg-gray-900 rounded p-3 text-xs overflow-x-auto text-gray-300">
          {JSON.stringify(bot.state, null, 2)}
        </pre>
      </div>

      <div>
        <h2 className="text-sm font-medium text-gray-400 mb-3 uppercase tracking-wide">
          Recent Audit (last 50)
        </h2>
        {bot.recent_audit.length === 0 ? (
          <p className="text-gray-500 text-sm">No audit rows.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead>
                <tr className="text-gray-400 border-b border-gray-700">
                  <th className="pb-2 pr-4">Time</th>
                  <th className="pb-2 pr-4">Kind</th>
                  <th className="pb-2 pr-4">Client Order ID</th>
                  <th className="pb-2">Payload</th>
                </tr>
              </thead>
              <tbody>
                {bot.recent_audit.map((row) => (
                  <tr key={row.id} className="border-b border-gray-800">
                    <td className="py-1.5 pr-4 text-gray-400">
                      {new Date(row.ts).toLocaleString()}
                    </td>
                    <td className="py-1.5 pr-4 font-mono">{row.kind}</td>
                    <td className="py-1.5 pr-4 font-mono text-gray-500">
                      {row.client_order_id ?? "-"}
                    </td>
                    <td className="py-1.5 text-gray-400 max-w-xs truncate">
                      {JSON.stringify(row.payload)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
