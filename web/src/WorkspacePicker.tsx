import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { postJson } from "./api.js";
import { rememberWorkspace, type WorkspaceStatus } from "./workspace.js";

export function WorkspacePicker({ value, roots, disabled = false, onChange }: {
  value: string; roots: string[]; disabled?: boolean; onChange: (selection: WorkspaceStatus) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [path, setPath] = useState(value);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const choose = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || disabled || !path.trim()) return;
    setSaving(true); setError("");
    try {
      const selected = await postJson<WorkspaceStatus>("/api/workbench/workspace/select", { workspace: path.trim() });
      rememberWorkspace(selected.workspace);
      onChange(selected);
      dialog.current?.close();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSaving(false); }
  };
  return <>
    <button type="button" className="change-folder" disabled={disabled || !value} title={disabled ? "Wait for the current task to finish before changing its folder" : "Choose a local workspace folder"} onClick={() => { setPath(value); setError(""); dialog.current?.showModal(); }}>Change folder</button>
    {createPortal(<dialog ref={dialog} className="folder-dialog" aria-labelledby="folder-dialog-title" onCancel={e => { if (saving) e.preventDefault(); }}>
      <form onSubmit={choose} aria-busy={saving}>
        <h2 id="folder-dialog-title">Change workspace folder</h2>
        <p>Choose where new tasks read source files and save their work.</p>
        <label htmlFor="folder-path">Folder path</label>
        <input id="folder-path" autoFocus autoComplete="off" spellCheck={false} value={path} disabled={saving} onChange={e => setPath(e.target.value)} placeholder="Paste the full path to a local folder" aria-describedby="folder-access" aria-invalid={Boolean(error)} required/>
        <small id="folder-access">Using this folder allows BlackBox to work with files inside it. Files are not moved or indexed when you switch.</small>
        {roots.length > 0 && <div className="folder-choices"><span>Available folders</span>{roots.map(root => <button type="button" key={root} title={root} disabled={saving} aria-pressed={path === root} onClick={() => { setPath(root); setError(""); }}>{root}</button>)}</div>}
        {error && <p className="inline-error" role="alert">{error}</p>}
        <footer><button type="button" disabled={saving} onClick={() => dialog.current?.close()}>Cancel</button><button className="primary" type="submit" disabled={saving || disabled || !path.trim()}>{saving ? "Checking folder…" : "Use folder"}</button></footer>
      </form>
    </dialog>, document.body)}
  </>;
}
