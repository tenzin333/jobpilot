import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/Layout";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type SettingsData } from "@/lib/api";
import { usePolling } from "@/lib/hooks";

const DEFAULTS: SettingsData = {
  dry_run: true,
  submit_kill_switch: false,
  scheduler_enabled: false,
  daily_submit_cap: 40,
  match_threshold: 70,
  cycle_interval_minutes: 60,
};

export default function Settings() {
  const { data, loading, error } = usePolling<SettingsData>(api.getSettings, null);

  const [form, setForm] = useState<SettingsData>(DEFAULTS);
  const [saving, setSaving] = useState(false);
  const seeded = useRef(false);

  // Seed editable state once the initial payload arrives.
  useEffect(() => {
    if (!data || seeded.current) return;
    seeded.current = true;
    setForm(data);
  }, [data]);

  function set<K extends keyof SettingsData>(key: K, value: SettingsData[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  // Coerce a number input, clamping to the backend's accepted range.
  function setNumber(key: keyof SettingsData, raw: string, min: number, max: number) {
    const n = Number(raw);
    if (Number.isNaN(n)) return set(key, 0 as SettingsData[typeof key]);
    set(key, Math.max(min, Math.min(max, Math.round(n))) as SettingsData[typeof key]);
  }

  async function onSave() {
    setSaving(true);
    try {
      const saved = await api.saveSettings(form);
      setForm(saved);
      toast.success("Settings saved");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <>
        <PageHeader title="Settings" />
        <div className="flex flex-col gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-40 rounded-xl" />
          ))}
        </div>
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader title="Settings" />
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load settings</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Runtime safety switches and limits. These override the .env defaults live — no restart needed."
        actions={
          <Button size="sm" onClick={onSave} disabled={saving}>
            {saving && <Loader2 className="animate-spin" />}
            Save changes
          </Button>
        }
      />

      <div className="flex flex-col gap-4">
        {/* Safety -------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>Safety</CardTitle>
            <CardDescription>
              Controls that gate whether applications are actually submitted.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <Toggle
              label="Dry run"
              description="Fill application forms but never click final submit. Recommended until you trust the flow."
              checked={form.dry_run}
              onChange={(v) => set("dry_run", v)}
            />
            <Toggle
              label="Kill switch"
              description="Block all submissions entirely, regardless of other settings."
              checked={form.submit_kill_switch}
              onChange={(v) => set("submit_kill_switch", v)}
              danger
            />
          </CardContent>
        </Card>

        {/* Limits -------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>Limits</CardTitle>
            <CardDescription>Caps and the match score required to apply.</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <NumberField
              label="Daily submit cap"
              hint="Max submissions per day"
              value={form.daily_submit_cap}
              min={0}
              max={1000}
              onChange={(v) => setNumber("daily_submit_cap", v, 0, 1000)}
            />
            <NumberField
              label="Match threshold"
              hint="0–100; minimum score to tailor + submit"
              value={form.match_threshold}
              min={0}
              max={100}
              onChange={(v) => setNumber("match_threshold", v, 0, 100)}
            />
          </CardContent>
        </Card>

        {/* Scheduler ----------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>Scheduler</CardTitle>
            <CardDescription>
              Automatically run discover → rank → tailor → submit cycles.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <Toggle
              label="Enable scheduler"
              description="Run the full pipeline on an interval instead of only on demand."
              checked={form.scheduler_enabled}
              onChange={(v) => set("scheduler_enabled", v)}
            />
            <NumberField
              label="Cycle interval (minutes)"
              hint="1–1440; how often the scheduler runs"
              value={form.cycle_interval_minutes}
              min={1}
              max={1440}
              onChange={(v) => setNumber("cycle_interval_minutes", v, 1, 1440)}
            />
          </CardContent>
        </Card>

        <div className="flex justify-end pb-4">
          <Button onClick={onSave} disabled={saving}>
            {saving && <Loader2 className="animate-spin" />}
            Save changes
          </Button>
        </div>
      </div>
    </>
  );
}

function Toggle({
  label,
  description,
  checked,
  onChange,
  danger,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  danger?: boolean;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3">
      <Checkbox
        checked={checked}
        onCheckedChange={(v) => onChange(v === true)}
        className={danger ? "mt-0.5 data-[state=checked]:bg-destructive data-[state=checked]:border-destructive" : "mt-0.5"}
      />
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-medium leading-none">{label}</span>
        <span className="text-[13px] text-muted-foreground">{description}</span>
      </div>
    </label>
  );
}

function NumberField({
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  onChange: (raw: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between">
        <Label>{label}</Label>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
      <Input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
