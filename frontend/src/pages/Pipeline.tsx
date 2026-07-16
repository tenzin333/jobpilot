import { useEffect, useState } from "react";
import { Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type PipelineSnapshot } from "@/lib/api";
import { usePolling } from "@/lib/hooks";

const STAGE_KEYS = [
  "discovered",
  "deduped",
  "ranked",
  "scored",
  "tailored",
  "submitted",
  "needs_human",
  "failed",
] as const;

export default function Pipeline() {
  const { data, error, loading, refresh } = usePolling<PipelineSnapshot>(api.pipelineStatus, null);
  const [starting, setStarting] = useState(false);
  const running = data?.running ?? false;

  // Poll live while a run is active.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(refresh, 1500);
    return () => clearInterval(id);
  }, [running, refresh]);

  async function onRun() {
    setStarting(true);
    try {
      const res = await api.pipelineRun();
      toast[res.started ? "success" : "message"](
        res.started ? "Pipeline started" : "A run is already in progress",
      );
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  const stats = data?.stats ?? {};
  const shownStats = STAGE_KEYS.filter((k) => k in stats);

  return (
    <>
      <PageHeader
        title="Pipeline"
        subtitle="Run a full cycle — discover → rank → tailor → submit — and watch it live."
        actions={
          <Button size="sm" onClick={onRun} disabled={starting || running}>
            {starting || running ? <Loader2 className="animate-spin" /> : <Play />}
            {running ? "Running…" : "Run full cycle"}
          </Button>
        }
      />

      {error && !data ? (
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load pipeline status</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between gap-3">
              <div>
                <CardTitle>Status</CardTitle>
                <CardDescription>
                  {loading ? "Loading…" : running ? "A cycle is in progress." : "Idle."}
                </CardDescription>
              </div>
              <Badge variant={running ? "default" : data?.error ? "destructive" : "secondary"}>
                {running && <Loader2 className="mr-1.5 size-3 animate-spin" />}
                {data?.stage ?? "idle"}
              </Badge>
            </CardHeader>
            {shownStats.length > 0 && (
              <CardContent className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {shownStats.map((k) => (
                  <div key={k} className="rounded-lg border border-border px-3 py-2.5">
                    <div className="text-[22px] font-semibold leading-none">{stats[k]}</div>
                    <div className="mt-1 text-[12px] capitalize text-muted-foreground">
                      {k.replace(/_/g, " ")}
                    </div>
                  </div>
                ))}
              </CardContent>
            )}
            {data?.error && (
              <CardContent>
                <p className="rounded-md bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
                  {data.error}
                </p>
              </CardContent>
            )}
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Live log</CardTitle>
              <CardDescription>Most recent activity from the running cycle.</CardDescription>
            </CardHeader>
            <CardContent>
              {data && data.logs.length > 0 ? (
                <pre className="max-h-[420px] overflow-auto rounded-lg bg-muted/50 p-3 font-mono text-[12.5px] leading-relaxed">
                  {data.logs.join("\n")}
                </pre>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No activity yet. Click <span className="font-medium">Run full cycle</span> to start.
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </>
  );
}
