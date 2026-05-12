interface RefreshIndicatorProps {
  isLoading: boolean;
  lastUpdated?: Date;
}

export function RefreshIndicator({ isLoading, lastUpdated }: RefreshIndicatorProps) {
  return (
    <div className="flex items-center gap-2 text-xs text-gray-500">
      {isLoading ? (
        <span className="inline-block w-2 h-2 bg-blue-400 rounded-full animate-pulse" />
      ) : (
        <span className="inline-block w-2 h-2 bg-gray-600 rounded-full" />
      )}
      {lastUpdated && (
        <span>Updated {lastUpdated.toLocaleTimeString()}</span>
      )}
    </div>
  );
}
