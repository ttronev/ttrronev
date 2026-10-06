import { LivePage } from "@/components/LivePage";
import { navById } from "@/lib/nav";

export default function Health() {
  return <LivePage entry={navById("health")} />;
}
