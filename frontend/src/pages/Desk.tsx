import { LivePage } from "@/components/LivePage";
import { navById } from "@/lib/nav";

export default function Desk() {
  return <LivePage entry={navById("desk")} />;
}
