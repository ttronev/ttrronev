import { PlannedPage } from "@/components/PlannedPage";
import { navById } from "@/lib/nav";

export default function Account() {
  return <PlannedPage entry={navById("account")} />;
}
