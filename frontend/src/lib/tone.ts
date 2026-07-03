import type { Tone } from "./api";

type BadgeVariant =
  | "default"
  | "secondary"
  | "destructive"
  | "outline"
  | "success"
  | "warning";

/** Map an API status "tone" to a shadcn Badge variant. */
export function toneVariant(tone: Tone): BadgeVariant {
  switch (tone) {
    case "success":
      return "success";
    case "warning":
      return "warning";
    case "danger":
      return "destructive";
    case "accent":
      return "default";
    default:
      return "secondary";
  }
}
