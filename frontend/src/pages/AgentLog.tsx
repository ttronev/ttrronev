import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function AgentLog() {
  return <PlannedPage entry={navById("agent-log")} />;
}
