import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function LiveLog() {
  return <PlannedPage entry={navById("live-log")} />;
}
