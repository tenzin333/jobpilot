import { useState } from "react";
import { CheckCircle2, ExternalLink, Loader2, MonitorPlay, Users } from "lucide-react";
import { toast } from "sonner";
import AssistPanel from "@/components/AssistPanel";
import { PageHeader } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type InterventionItem } from "@/lib/api";
import { usePolling } from "@/lib/hooks";

function Item({
  item,
  onDone,
  onAssist,
}: {
  item: InterventionItem;
  onDone: (id: number) => void;
  onAssist: (item: InterventionItem) => void;
}) {
  const [saving, setSaving] = useState(false);

  async function markDone() {
    setSaving(true);
    try {
      await api.interventionDone(item.id);
      toast.success("Marked as submitted");
      onDone(item.id);
    } catch (e) {
      toast.error((e as Error).message);
      setSaving(false);
    }
  }

  // Co-browse is only supported for the schema-driven Ashby ATS; others just
  // open the posting in a new tab.
  const canCoBrowse = item.ats_type === "ashby";

  return (
    <Card className="gap-0 py-0">
      <CardContent className="flex items-center gap-4 px-4 py-3.5">
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-semibold">{item.title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[12.5px] text-muted-foreground">
            <span>{item.company}</span>
            <span className="text-border">·</span>
            <Badge variant="outline" className="font-normal">
              {item.ats_type || item.source}
            </Badge>
          </div>
          {item.reason && (
            <p className="mt-1 text-[12.5px] text-warning">Needs you: {item.reason}</p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {canCoBrowse && (
            <Button variant="secondary" size="sm" onClick={() => onAssist(item)}>
              <MonitorPlay className="size-3.5" /> Co-browse
            </Button>
          )}
          {item.apply_url && (
            <Button asChild variant="outline" size="sm">
              <a href={item.apply_url} target="_blank" rel="noopener noreferrer">
                Open posting <ExternalLink className="size-3.5" />
              </a>
            </Button>
          )}
          <Button size="sm" onClick={markDone} disabled={saving}>
            {saving ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
            Mark done
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default function Intervention() {
  const { data, error, loading, refresh } = usePolling(api.intervention, null);
  const [assist, setAssist] = useState<InterventionItem | null>(null);
  const items = data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Intervention"
        subtitle="Applications that need you — captchas, essays, video intros, or unsupported forms. Finish them, then mark done."
      />

      {assist && (
        <div className="mb-4">
          <AssistPanel
            appId={assist.id}
            title={`${assist.title} · ${assist.company}`}
            onClose={() => setAssist(null)}
          />
        </div>
      )}

      {loading ? (
        <div className="flex flex-col gap-2.5">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[76px] rounded-xl" />
          ))}
        </div>
      ) : error ? (
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load the queue</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      ) : items.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-14 text-center">
            <Users className="mx-auto mb-2 size-5 text-muted-foreground" />
            <p className="font-medium">Nothing needs you</p>
            <p className="mt-1.5 text-sm text-muted-foreground">
              Applications that require manual steps will appear here.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-2.5">
          {items.map((item) => (
            <Item
              key={item.id}
              item={item}
              onDone={() => {
                if (assist?.id === item.id) setAssist(null);
                refresh();
              }}
              onAssist={setAssist}
            />
          ))}
        </div>
      )}
    </>
  );
}
