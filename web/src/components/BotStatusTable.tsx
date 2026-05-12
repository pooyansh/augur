import type { StatusBot } from "../lib/api.ts";

interface BotStatusTableProps {
  bots: StatusBot[];
}

function ageBadge(ageS: number) {
  if (ageS < 60) return <span className="text-green-400">{ageS.toFixed(0)}s</span>;
  if (ageS < 180) return <span className="text-yellow-400">{ageS.toFixed(0)}s</span>;
  return <span className="text-red-400">{ageS.toFixed(0)}s</span>;
}

export function BotStatusTable({ bots }: BotStatusTableProps) {
  if (bots.length === 0) {
    return <p className="text-gray-500 text-sm">No bots running.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm text-left">
        <thead>
          <tr className="text-gray-400 border-b border-gray-700">
            <th className="pb-2 pr-4">Bot ID</th>
            <th className="pb-2 pr-4">Strategy</th>
            <th className="pb-2 pr-4">Mode</th>
            <th className="pb-2 pr-4">PID</th>
            <th className="pb-2 pr-4">Restarts</th>
            <th className="pb-2 pr-4">Heartbeat</th>
            <th className="pb-2">Last Error</th>
          </tr>
        </thead>
        <tbody>
          {bots.map((bot) => (
            <tr key={bot.bot_id} className="border-b border-gray-800 hover:bg-gray-800/40">
              <td className="py-2 pr-4 font-mono text-xs">{bot.bot_id}</td>
              <td className="py-2 pr-4">{bot.strategy}</td>
              <td className="py-2 pr-4">
                <span
                  className={`px-2 py-0.5 rounded text-xs font-medium ${
                    bot.mode === "live"
                      ? "bg-amber-900 text-amber-200"
                      : "bg-indigo-900 text-indigo-200"
                  }`}
                >
                  {bot.mode}
                </span>
              </td>
              <td className="py-2 pr-4 font-mono text-xs">
                {bot.pid ?? <span className="text-red-400">dead</span>}
              </td>
              <td className="py-2 pr-4">{bot.restart_count}</td>
              <td className="py-2 pr-4">{ageBadge(bot.heartbeat_age_s)}</td>
              <td className="py-2 text-xs text-red-300 max-w-xs truncate">
                {bot.last_error ?? "-"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
