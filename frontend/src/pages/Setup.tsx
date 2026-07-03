import { useEffect, useRef, useState } from "react";
import { FileText, Loader2, Upload } from "lucide-react";
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
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api, type SetupData, type SetupProfile } from "@/lib/api";
import { usePolling } from "@/lib/hooks";

// --- Answer-bank field catalogue ------------------------------------------
// Each key is stored verbatim in the answer bank and matched (by the LLM) to
// application-form fields, so keys read like the questions portals actually ask.
type BankField = {
  key: string;
  label: string;
  type?: "text" | "select";
  options?: string[];
  placeholder?: string;
  span2?: boolean;
};

const YES_NO = ["Yes", "No"];
const PNTS = "Prefer not to say";

const BANK_SECTIONS: { title: string; description: string; fields: BankField[] }[] = [
  {
    title: "Links & profiles",
    description: "Public profiles most portals ask for.",
    fields: [
      { key: "linkedin", label: "LinkedIn URL", placeholder: "https://linkedin.com/in/…" },
      { key: "github", label: "GitHub URL", placeholder: "https://github.com/…" },
      { key: "portfolio", label: "Portfolio / website", placeholder: "https://…" },
      { key: "twitter", label: "Twitter / X", placeholder: "https://x.com/…" },
    ],
  },
  {
    title: "Personal details",
    description: "Contact and location fields.",
    fields: [
      { key: "current_location", label: "Current location", placeholder: "City, State" },
      { key: "city", label: "City" },
      { key: "state", label: "State / Province" },
      { key: "country", label: "Country" },
      { key: "postal code", label: "Postal / ZIP code" },
      { key: "pronouns", label: "Pronouns", placeholder: "she/her, he/him, they/them" },
    ],
  },
  {
    title: "Work eligibility",
    description: "Authorization and availability questions.",
    fields: [
      { key: "authorized to work", label: "Authorized to work in the country?", type: "select", options: YES_NO },
      { key: "require sponsorship", label: "Require visa sponsorship (now or future)?", type: "select", options: YES_NO },
      { key: "over 18", label: "At least 18 years old?", type: "select", options: YES_NO },
      { key: "willing to relocate", label: "Willing to relocate?", type: "select", options: YES_NO },
      { key: "open to remote", label: "Open to remote work?", type: "select", options: ["Yes", "No", "Hybrid"] },
      { key: "security clearance", label: "Security clearance", placeholder: "None / Secret / …" },
    ],
  },
  {
    title: "Experience & compensation",
    description: "Employment history and pay expectations.",
    fields: [
      { key: "years_experience", label: "Years of experience", placeholder: "e.g. 5" },
      { key: "current_employer", label: "Current / most recent employer" },
      { key: "current_title", label: "Current / most recent job title" },
      { key: "current salary", label: "Current salary" },
      { key: "salary_expectation", label: "Desired salary" },
      { key: "notice period", label: "Notice period", placeholder: "e.g. 2 weeks" },
      { key: "earliest start date", label: "Earliest start date", placeholder: "e.g. 2 weeks" },
    ],
  },
  {
    title: "Education",
    description: "Highest qualification.",
    fields: [
      { key: "highest degree", label: "Highest degree", placeholder: "e.g. B.Sc. Computer Science" },
      { key: "university", label: "University / school" },
      { key: "field of study", label: "Field of study / major" },
      { key: "graduation year", label: "Graduation year" },
      { key: "gpa", label: "GPA" },
    ],
  },
  {
    title: "Voluntary self-identification (EEO)",
    description: "Optional demographic questions common on US portals.",
    fields: [
      { key: "gender", label: "Gender", type: "select", options: ["Male", "Female", "Non-binary", PNTS] },
      { key: "hispanic or latino", label: "Hispanic or Latino?", type: "select", options: ["Yes", "No", PNTS] },
      { key: "race or ethnicity", label: "Race / ethnicity" },
      { key: "veteran status", label: "Veteran status", type: "select", options: ["I am not a protected veteran", "I am a protected veteran", PNTS], span2: true },
      { key: "disability status", label: "Disability status", type: "select", options: ["No", "Yes", PNTS] },
    ],
  },
  {
    title: "Screening",
    description: "Miscellaneous questions applications throw in.",
    fields: [
      { key: "how did you hear about us", label: "How did you hear about us?" },
      { key: "referred by", label: "Referred by (name)" },
      { key: "driver's license", label: "Valid driver's license?", type: "select", options: YES_NO },
      { key: "background check", label: "Consent to a background check?", type: "select", options: YES_NO },
      { key: "drug test", label: "Consent to a drug test?", type: "select", options: YES_NO },
    ],
  },
];

const KNOWN_KEYS = new Set(BANK_SECTIONS.flatMap((s) => s.fields.map((f) => f.key)));

const splitLines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);
const joinLines = (items: string[]) => items.join("\n");

/** Split a saved answer bank into known field values + free-form leftovers. */
function partitionBank(bank: Record<string, string>) {
  const known: Record<string, string> = {};
  const extra: string[] = [];
  for (const [k, v] of Object.entries(bank)) {
    if (KNOWN_KEYS.has(k)) known[k] = v;
    else extra.push(`${k}: ${v}`);
  }
  return { known, extra: extra.join("\n") };
}

export default function Setup() {
  const { data, loading, error } = usePolling<SetupData>(api.getSetup, null);

  const [profile, setProfile] = useState<SetupProfile | null>(null);
  const [bank, setBank] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState("");
  const [roles, setRoles] = useState("");
  const [locations, setLocations] = useState("");
  const [remote, setRemote] = useState("any");
  const [minSalary, setMinSalary] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [sponsorship, setSponsorship] = useState(false);
  const [workAuth, setWorkAuth] = useState("");
  const [greenhouse, setGreenhouse] = useState("");
  const [lever, setLever] = useState("");
  const [remoteOptions, setRemoteOptions] = useState<string[]>([]);

  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const seeded = useRef(false);

  // Seed editable state once the initial payload arrives.
  useEffect(() => {
    if (!data || seeded.current) return;
    seeded.current = true;
    const p = data.preferences;
    setProfile(data.profile);
    setRoles(joinLines(p.desired_roles));
    setLocations(joinLines(p.locations));
    setRemote(p.remote_preference);
    setMinSalary(p.min_salary != null ? String(p.min_salary) : "");
    setCurrency(p.salary_currency);
    setSponsorship(p.require_sponsorship);
    setWorkAuth(p.work_authorization);
    setGreenhouse(joinLines(p.greenhouse_companies));
    setLever(joinLines(p.lever_companies));
    setRemoteOptions(data.remote_options);
    const { known, extra } = partitionBank(data.answer_bank);
    setBank(known);
    setExtra(extra);
  }, [data]);

  function setField(key: string, value: string) {
    setBank((b) => ({ ...b, [key]: value }));
  }

  function buildBank(): Record<string, string> {
    const out: Record<string, string> = { ...bank };
    for (const line of extra.split("\n")) {
      const idx = line.indexOf(":");
      if (idx > 0) {
        const k = line.slice(0, idx).trim();
        const v = line.slice(idx + 1).trim();
        if (k && v) out[k] = v;
      }
    }
    return out;
  }

  async function onSave() {
    setSaving(true);
    try {
      await api.saveSetup({
        preferences: {
          desired_roles: splitLines(roles),
          locations: splitLines(locations),
          remote_preference: remote,
          min_salary: minSalary.trim() ? Number(minSalary) : null,
          salary_currency: currency || "USD",
          require_sponsorship: sponsorship,
          work_authorization: workAuth,
          greenhouse_companies: splitLines(greenhouse),
          lever_companies: splitLines(lever),
        },
        answer_bank: buildBank(),
      });
      toast.success("Setup saved");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const res = await api.uploadResume(file);
      setProfile(res.profile);
      const { known, extra } = partitionBank(res.answer_bank);
      setBank(known);
      setExtra(extra);
      toast.success(`Parsed ${file.name}`);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  if (loading) {
    return (
      <>
        <PageHeader title="Setup" />
        <div className="flex flex-col gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-40 rounded-xl" />
          ))}
        </div>
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader title="Setup" />
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load setup</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Setup"
        subtitle="Résumé, job-search preferences, and the answers used to fill application forms."
        actions={
          <Button size="sm" onClick={onSave} disabled={saving}>
            {saving && <Loader2 className="animate-spin" />}
            Save changes
          </Button>
        }
      />

      <div className="flex flex-col gap-4">
        {/* Résumé -------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>Base résumé</CardTitle>
            <CardDescription>
              Upload a .pdf or .docx. We parse it to prefill your profile and answers.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-4">
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx"
              className="hidden"
              onChange={onUpload}
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? <Loader2 className="animate-spin" /> : <Upload />}
              {uploading ? "Parsing…" : "Upload résumé"}
            </Button>

            {profile ? (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                <span className="font-medium">{profile.full_name || "(name not parsed)"}</span>
                <span className="text-muted-foreground">{profile.email || "no email"}</span>
                <span className="text-muted-foreground">
                  {profile.skills} skills · {profile.experience} roles · {profile.education} education
                </span>
                {profile.resume_filename && (
                  <Badge variant="secondary" className="gap-1.5">
                    <FileText className="size-3" />
                    {profile.resume_filename}
                  </Badge>
                )}
              </div>
            ) : (
              <span className="text-sm text-muted-foreground">No résumé uploaded yet.</span>
            )}
          </CardContent>
        </Card>

        {/* Preferences --------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>Job-search preferences</CardTitle>
            <CardDescription>Drives what the agent discovers and ranks.</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <Field label="Desired roles" hint="One per line" span2>
              <Textarea
                rows={3}
                value={roles}
                onChange={(e) => setRoles(e.target.value)}
                placeholder={"Software Engineer\nBackend Engineer"}
              />
            </Field>
            <Field label="Locations" hint='One per line; use "Remote"'>
              <Textarea
                rows={3}
                value={locations}
                onChange={(e) => setLocations(e.target.value)}
                placeholder={"Remote\nNew York, NY"}
              />
            </Field>
            <Field label="Remote preference">
              <Select value={remote} onValueChange={setRemote}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {remoteOptions.map((o) => (
                    <SelectItem key={o} value={o}>
                      {o.replace(/_/g, " ")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Minimum salary">
              <Input
                type="number"
                value={minSalary}
                onChange={(e) => setMinSalary(e.target.value)}
                placeholder="e.g. 120000"
              />
            </Field>
            <Field label="Salary currency">
              <Input value={currency} onChange={(e) => setCurrency(e.target.value)} />
            </Field>
            <Field label="Work authorization" span2>
              <Input
                value={workAuth}
                onChange={(e) => setWorkAuth(e.target.value)}
                placeholder="e.g. US Citizen, Green Card, requires H-1B"
              />
            </Field>
            <label className="flex items-center gap-2.5 md:col-span-2">
              <Checkbox
                checked={sponsorship}
                onCheckedChange={(v) => setSponsorship(v === true)}
              />
              <span className="text-sm">I require visa sponsorship</span>
            </label>
          </CardContent>
        </Card>

        {/* Answer bank sections ----------------------------------------- */}
        {BANK_SECTIONS.map((section) => (
          <Card key={section.title}>
            <CardHeader>
              <CardTitle>{section.title}</CardTitle>
              <CardDescription>{section.description}</CardDescription>
            </CardHeader>
            <CardContent className="grid grid-cols-1 gap-5 md:grid-cols-2">
              {section.fields.map((f) => (
                <Field key={f.key} label={f.label} span2={f.span2}>
                  {f.type === "select" ? (
                    <Select
                      value={bank[f.key] ?? ""}
                      onValueChange={(v) => setField(f.key, v)}
                    >
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder="Select…" />
                      </SelectTrigger>
                      <SelectContent>
                        {f.options!.map((o) => (
                          <SelectItem key={o} value={o}>
                            {o}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <Input
                      value={bank[f.key] ?? ""}
                      placeholder={f.placeholder}
                      onChange={(e) => setField(f.key, e.target.value)}
                    />
                  )}
                </Field>
              ))}
            </CardContent>
          </Card>
        ))}

        {/* Additional custom answers ------------------------------------ */}
        <Card>
          <CardHeader>
            <CardTitle>Additional answers</CardTitle>
            <CardDescription>
              Anything else, one per line as <code>question: answer</code>. These fill
              any form field the LLM can match by label.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Textarea
              rows={5}
              value={extra}
              onChange={(e) => setExtra(e.target.value)}
              placeholder={"preferred name: Sam\nt-shirt size: M"}
              className="font-mono text-[13px]"
            />
          </CardContent>
        </Card>

        {/* Sources ------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle>Sources</CardTitle>
            <CardDescription>Company board slugs to discover jobs from.</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <Field label="Greenhouse company slugs" hint="One per line">
              <Textarea
                rows={4}
                value={greenhouse}
                onChange={(e) => setGreenhouse(e.target.value)}
                placeholder={"stripe\nairbnb"}
              />
            </Field>
            <Field label="Lever company slugs" hint="One per line">
              <Textarea
                rows={4}
                value={lever}
                onChange={(e) => setLever(e.target.value)}
                placeholder={"netflix\nspotify"}
              />
            </Field>
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

function Field({
  label,
  hint,
  span2,
  children,
}: {
  label: string;
  hint?: string;
  span2?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={`flex flex-col gap-1.5 ${span2 ? "md:col-span-2" : ""}`}>
      <div className="flex items-baseline justify-between">
        <Label>{label}</Label>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
      {children}
    </div>
  );
}
