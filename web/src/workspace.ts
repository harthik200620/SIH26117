export type WorkspaceStatus = { workspace: string; roots: string[] };
const key = "blackbox.workspace";
export function preferredWorkspace(status: WorkspaceStatus): string {
  try {
    const chosen = sessionStorage.getItem("blackbox.draftWorkspace") || localStorage.getItem(key);
    if (chosen && status.roots.includes(chosen)) return chosen;
  } catch { /* Storage may be disabled by browser policy. */ }
  return status.workspace;
}
export function rememberWorkspace(path: string): void {
  try {
    localStorage.setItem(key, path);
    sessionStorage.removeItem("blackbox.draftWorkspace");
  } catch { /* The current selection still works without persistence. */ }
}
