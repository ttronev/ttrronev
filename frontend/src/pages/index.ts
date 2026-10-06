import type { ComponentType } from "react";
import Account from "@/pages/Account";
import AgentLog from "@/pages/AgentLog";
import Backtests from "@/pages/Backtests";
import Bots from "@/pages/Bots";
import Build from "@/pages/Build";
import Chat from "@/pages/Chat";
import Connections from "@/pages/Connections";
import Desk from "@/pages/Desk";
import Health from "@/pages/Health";
import LiveLog from "@/pages/LiveLog";
import Logs from "@/pages/Logs";
import Settings from "@/pages/Settings";
import Strategies from "@/pages/Strategies";

/** nav id -> page component (one page per sidebar entry). */
export const PAGES: Record<string, ComponentType> = {
  desk: Desk,
  strategies: Strategies,
  backtests: Backtests,
  bots: Bots,
  "live-log": LiveLog,
  connections: Connections,
  chat: Chat,
  account: Account,
  health: Health,
  logs: Logs,
  build: Build,
  "agent-log": AgentLog,
  settings: Settings,
};
