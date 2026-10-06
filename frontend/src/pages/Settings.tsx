import { LivePage } from "@/components/LivePage";
import { navById } from "@/lib/nav";

export default function Settings() {
  return <LivePage entry={navById("settings")} />;
}
