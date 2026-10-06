import { LivePage } from "@/components/LivePage";
import { navById } from "@/lib/nav";

export default function Logs() {
  return <LivePage entry={navById("logs")} />;
}
