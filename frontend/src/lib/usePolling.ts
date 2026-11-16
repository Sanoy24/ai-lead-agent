import { useEffect } from "react";

/** Calls `refresh` every `ms` milliseconds while the tab is visible. */
export function usePolling(refresh: () => void, ms: number): void {
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, ms);
    return () => window.clearInterval(id);
  }, [refresh, ms]);
}
