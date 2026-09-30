export type SectionKey = "workspace" | "documents" | "assurance" | "settings";
export const sections = {
  workspace: { label: "Workspace", description: "Ask, investigate, and create with your local assistant.", tabs: { chat: "Assistant", history: "Task history" } },
  documents: { label: "Documents", description: "Find local evidence and bring the right sources into your work.", tabs: { library: "Files & search" } },
  assurance: { label: "Assurance", description: "Inspect network controls, action records, and measured results.", tabs: { network: "Network & privacy", activity: "Activity log", evaluations: "Evaluations" } },
  settings: { label: "Settings", description: "Manage local models, automatic routing, and this installation.", tabs: { models: "Models & routing", runtime: "Runtime & logs" } },
} as const;
export type Route = { section: SectionKey; tab: string };
const aliases: Record<string, string> = {
  chat: "workspace/chat", runs: "workspace/history", knowledge: "documents/library",
  seal: "assurance/network", audit: "assurance/activity", evals: "assurance/evaluations",
  models: "settings/models", settings: "settings/runtime",
};
export function readRoute(hash: string): Route {
  const raw = hash.replace(/^#/, "");
  const [section, tab] = (Object.hasOwn(aliases, raw) ? aliases[raw]! : raw).split("/");
  if (section && Object.hasOwn(sections, section)) {
    const key = section as SectionKey;
    const tabs = sections[key].tabs;
    return { section: key, tab: tab && Object.hasOwn(tabs, tab) ? tab : (Object.keys(tabs)[0] ?? "chat") };
  }
  return { section: "workspace", tab: "chat" };
}
export function routeHash(route: Route): string { return `#${route.section}/${route.tab}`; }
