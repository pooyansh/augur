import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api.ts";
import { useVisibleInterval } from "../lib/refresh.ts";
import { KpiCard } from "../components/KpiCard.tsx";
import { BotStatusTable } from "../components/BotStatusTable.tsx";
import { RefreshIndicator } from "../components/RefreshIndicator.tsx";
import { useState } from "react";

export default function Overview() {
  const [lastUpdated, setLastUpdated] = useState<Date | undefined>();

  const statusQ = useQuery({
    queryKey: ["status"],
    queryFn: () => api.status(),
    refetchInterval: false, // manual via useVisibleInterval
  });

  const capitalQ = useQuery({
    queryKey: ["capital"],
    queryFn: () => api.capital(),
    refetchInterval: 60_000,
  });

  const healthQ = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(),
    refetchInterval: false,
  });

  useVisibleInterval(() => {
    void statusQ.refetch();
    void healthQ.refetch();
    setLastUpdated(new Date());
  }, 2_000);

  const status = statusQ.data;
  const capital = capitalQ.data;
  const health = healthQ.data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Overview</h1>
        <RefreshIndicator isLoading={statusQ.isFetching} lastUpdated={lastUpdated} />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KpiCard
          label="Total Bots"
          value={status?.total_bots ?? "—"}
          color="default"
        />
        <KpiCard
          label="Alive"
          value={status?.alive_bots ?? "—"}
          color={status && status.alive_bots === status.total_bots ? "green" : "red"}
        />
        <KpiCard
          label="Capital (USD)"
          value={capital ? `$${capital.total_usd.toFixed(2)}` : "—"}
          sub={capital?.sourced_from.includes("pending") ? "trailing exchange" : undefined}
          color="blue"
        />
        <KpiCard
          label="Postgres"
          value={health ? (health.postgres_ok ? "OK" : "DOWN") : "—"}
          color={health?.postgres_ok ? "green" : "red"}
        />
      </div>

      <div>
        <h2 className="text-sm font-medium text-gray-400 mb-3 uppercase tracking-wide">
          Bot Status
        </h2>
        {status ? (
          <BotStatusTable bots={status.bots} />
        ) : (
          <p className="text-gray-500 text-sm">Loading...</p>
        )}
      </div>
    </div>
  );
}
