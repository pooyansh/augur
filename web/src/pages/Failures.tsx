import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api.ts";
import { FailureTimeline } from "../components/FailureTimeline.tsx";

export default function Failures() {
  const q = useQuery({
    queryKey: ["failures"],
    queryFn: () => api.failures(),
    refetchInterval: false,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Failures (last 7 days)</h1>
        <button
          onClick={() => void q.refetch()}
          className="bg-gray-700 hover:bg-gray-600 text-sm px-3 py-1.5 rounded"
        >
          Refresh
        </button>
      </div>

      {q.isLoading && <p className="text-gray-500 text-sm">Loading...</p>}

      {q.data && (
        <p className="text-sm text-gray-400">
          {q.data.total} event{q.data.total !== 1 ? "s" : ""}
        </p>
      )}

      {q.data && <FailureTimeline events={q.data.events} />}
    </div>
  );
}
