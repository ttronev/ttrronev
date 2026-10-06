import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function Bots() {
  return <PlannedPage entry={navById("bots")} />;
}
