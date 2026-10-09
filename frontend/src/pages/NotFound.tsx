import { Link } from "react-router";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Page not found" description="There is no screen at this address." />
      <div>
        <Button asChild variant="outline">
          <Link to="/">Back to Desk</Link>
        </Button>
      </div>
    </div>
  );
}
