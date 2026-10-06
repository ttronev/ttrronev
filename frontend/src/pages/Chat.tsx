import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function Chat() {
  return <PlannedPage entry={navById("chat")} />;
}
