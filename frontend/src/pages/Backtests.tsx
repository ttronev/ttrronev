import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function Backtests() {
  return <PlannedPage entry={navById("backtests")} />;
}
