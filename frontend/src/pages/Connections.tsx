import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function Connections() {
  return <PlannedPage entry={navById("connections")} />;
}
