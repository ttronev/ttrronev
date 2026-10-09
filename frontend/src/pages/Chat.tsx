// Chat (B8): the agent, aware of the screen you are on. It drafts
// strategies into the Builder, explains a scorecard, proposes changes. It
// cannot edit numbers or place orders.
import { PlannedAction, PlannedPage } from "@/components/PlannedPage";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { navById } from "@/lib/nav";
import { CHAT } from "@/mock/planned";
import { cn } from "@/lib/utils";

export default function Chat() {
  const entry = navById("chat");
  const stage = entry.stage ?? "B8";
  return (
    <PlannedPage entry={entry}>
      <div className="text-xs text-muted-foreground" data-testid="chat-context">
        screen context: Desk · SOL/USDT · 1H
      </div>
      <Card>
        <CardContent className="flex flex-col gap-3 p-4" data-testid="chat-thread">
          {CHAT.map((m, i) => (
            <div key={i} data-testid="chat-message" data-role={m.role} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[80%] rounded-lg px-3 py-2 text-sm",
                  m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted text-foreground",
                )}
              >
                <div className="mb-0.5 text-[10px] uppercase tracking-wider opacity-70">
                  {m.role} · {m.ts}
                </div>
                {m.text}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
      <div className="flex items-end gap-2">
        <Textarea disabled placeholder="Ask about this screen, draft a strategy, explain a scorecard…" className="min-h-[44px]" />
        <PlannedAction label="Send" stage={stage} />
      </div>
      <p className="text-xs text-muted-foreground">The agent explains and drafts; it never edits a number or places an order.</p>
    </PlannedPage>
  );
}
