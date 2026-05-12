import type { FailureEvent } from "../lib/api.ts";

interface FailureTimelineProps {
  events: FailureEvent[];
}

const kindColor: Record<string, string> = {
  order_rejected: "bg-red-900 text-red-200",
  bot_crash: "bg-red-900 text-red-200",
  bot_cooldown: "bg-orange-900 text-orange-200",
  kill_switch_tripped: "bg-red-950 text-red-100 font-bold",
  signal_stale: "bg-yellow-900 text-yellow-200",
  snapshot_failed: "bg-orange-900 text-orange-200",
  live_downgrade: "bg-purple-900 text-purple-200",
};

function kindBadge(kind: string) {
  const cls = kindColor[kind] ?? "bg-gray-800 text-gray-300";
  return <span className={`px-2 py-0.5 rounded text-xs ${cls}`}>{kind}</span>;
}

export function FailureTimeline({ events }: FailureTimelineProps) {
  if (events.length === 0) {
    return <p className="text-gray-500 text-sm">No failures in the last 7 days.</p>;
  }

  return (
    <ul className="space-y-2">
      {events.map((ev, i) => (
        <li key={i} className="flex gap-3 items-start text-sm">
          <span className="text-gray-500 text-xs font-mono whitespace-nowrap pt-0.5">
            {new Date(ev.ts).toLocaleString()}
          </span>
          <span className="text-gray-400 font-mono text-xs pt-0.5">{ev.bot_id}</span>
          {kindBadge(ev.kind)}
          {ev.detail && (
            <span className="text-gray-400 truncate max-w-xs">{ev.detail}</span>
          )}
        </li>
      ))}
    </ul>
  );
}
