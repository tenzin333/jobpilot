import { useCallback, useEffect, useRef, useState } from "react";

interface FetchState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * Fetch `fetcher()` on mount and optionally re-poll on an interval. Returns the
 * data, a loading flag (only true on the very first load), any error, and a
 * `refresh()` to re-run on demand. Set `intervalMs` to enable polling; pass a
 * function/null via `enabled` to pause it (e.g. only poll while a run is active).
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs?: number | null,
  deps: unknown[] = [],
): FetchState<T> & { refresh: () => Promise<void> } {
  const [state, setState] = useState<FetchState<T>>({
    data: null,
    error: null,
    loading: true,
  });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      const data = await fetcherRef.current();
      setState({ data, error: null, loading: false });
    } catch (e) {
      setState((s) => ({ ...s, error: (e as Error).message, loading: false }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    refresh();
    if (!intervalMs) return;
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  return { ...state, refresh };
}
