import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api.ts";

export default function Audit() {
  const [botFilter, setBotFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");

  const q = useQuery({
    queryKey: ["audit", botFilter, kindFilter],
    queryFn: () =>
      api.audit({
        limit: 100,
        bot_id: botFilter || undefined,
        kind: kindFilter || undefined,
      }),
    refetchInterval: false,
  });

  const rows = q.data ?? [];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Audit Log</h1>

      <div className="flex gap-3 flex-wrap">
        <input
          className="bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-sm focus:outline-none focus:border-blue-500"
          placeholder="Filter by bot_id"
          value={botFilter}
          onChange={(e) => setBotFilter(e.target.value)}
        />
        <input
          className="bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-sm focus:outline-none focus:border-blue-500"
          placeholder="Filter by kind"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
        />
        <button
          onClick={() => void q.refetch()}
          className="bg-blue-700 hover:bg-blue-600 text-white px-4 py-1.5 rounded text-sm"
        >
          Refresh
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-2 pr-4">Time</th>
              <th className="pb-2 pr-4">Bot</th>
              <th className="pb-2 pr-4">Kind</th>
              <th className="pb-2 pr-4">Client Order ID</th>
              <th className="pb-2">Payload</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-gray-800 hover:bg-gray-800/40">
                <td className="py-1.5 pr-4 text-gray-400">
                  {new Date(row.ts).toLocaleString()}
                </td>
                <td className="py-1.5 pr-4 font-mono">{row.bot_id}</td>
                <td className="py-1.5 pr-4 font-mono">{row.kind}</td>
                <td className="py-1.5 pr-4 font-mono text-gray-500">
                  {row.client_order_id ?? "-"}
                </td>
                <td className="py-1.5 max-w-xs truncate text-gray-400">
                  {JSON.stringify(row.payload)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
