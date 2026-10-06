import { LivePage } from "@/components/LivePage";
import { navById } from "@/lib/nav";

export default function Build() {
  return <LivePage entry={navById("build")} />;
}
