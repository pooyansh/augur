/**
 * Visibility-aware polling hook.
 *
 * Calls `callback` every `intervalMs` milliseconds, but pauses when the
 * document is hidden (tab in background).  Resumes immediately when the
 * document becomes visible again.
 */

import { useEffect, useRef } from "react";

export function useVisibleInterval(callback: () => void, intervalMs: number) {
  const savedCallback = useRef(callback);

  // Always keep the ref pointing at the latest callback.
  useEffect(() => {
    savedCallback.current = callback;
  }, [callback]);

  useEffect(() => {
    if (intervalMs <= 0) return;

    let timerId: ReturnType<typeof setInterval> | null = null;

    function start() {
      if (timerId !== null) return;
      // Run immediately on start, then on each interval.
      savedCallback.current();
      timerId = setInterval(() => savedCallback.current(), intervalMs);
    }

    function stop() {
      if (timerId !== null) {
        clearInterval(timerId);
        timerId = null;
      }
    }

    function onVisibilityChange() {
      if (document.hidden) {
        stop();
      } else {
        start();
      }
    }

    document.addEventListener("visibilitychange", onVisibilityChange);

    if (!document.hidden) {
      start();
    }

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [intervalMs]);
}
