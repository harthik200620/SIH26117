
const paths = {
  workspace: <><path d="M4 4h16v12H9l-5 4V4Z"/><path d="M8 8h8M8 12h5"/></>,
  documents: <><path d="M6 3h8l4 4v14H6V3Z"/><path d="M14 3v5h4M9 12h6M9 16h6"/></>,
  assurance: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/></>,
  settings: <><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  arrow: <path d="M5 12h14m-5-5 5 5-5 5"/>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
} as const;
export function Icon({ name, size = 20 }: { name: keyof typeof paths; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
