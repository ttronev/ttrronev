import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function Strategies() {
  return <PlannedPage entry={navById("strategies")} />;
}
