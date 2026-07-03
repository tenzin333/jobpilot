import { PageHeader } from "@/components/Layout";
import { Card, CardContent } from "@/components/ui/card";

export default function Placeholder({ title, note }: { title: string; note: string }) {
  return (
    <>
      <PageHeader title={title} subtitle={note} />
      <Card className="border-dashed">
        <CardContent className="py-14 text-center">
          <p className="font-medium">Coming to the new console</p>
          <p className="mx-auto mt-1.5 max-w-md text-sm text-muted-foreground">
            This page hasn’t been migrated yet. It’s still available in the classic
            interface while we rebuild it here.
          </p>
        </CardContent>
      </Card>
    </>
  );
}
