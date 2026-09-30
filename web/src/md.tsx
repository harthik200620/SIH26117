import React from "react";

/* Minimal markdown for agent output: fenced code, inline code, bold/italic,
   headings, lists, links. Hand-rolled — no external deps inside the seal. */

function inline(text: string, keyBase: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyBase}-${i++}`;
    if (tok.startsWith("`")) out.push(<code className="md-code" key={key}>{tok.slice(1, -1)}</code>);
    else if (tok.startsWith("**")) out.push(<b key={key}>{tok.slice(2, -2)}</b>);
    else if (tok.startsWith("*")) out.push(<i key={key}>{tok.slice(1, -1)}</i>);
    else {
      const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok);
      if (mm) {
        // Model-supplied URLs can embed confidential text. Work output must not
        // turn them into an outbound navigation path; setup links live separately.
        if (mm[2]?.startsWith("#")) out.push(<a key={key} href={mm[2]}>{mm[1]}</a>);
        else out.push(<span key={key} title="Navigation from generated content is disabled">{mm[1]} ({mm[2]})</span>);
      }
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text }: { text: string }): React.ReactElement {
  const blocks: React.ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.startsWith("```")) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !(lines[i] ?? "").startsWith("```")) buf.push(lines[i++] ?? "");
      i++;
      blocks.push(<pre className="md-fence" key={key++}>{buf.join("\n")}</pre>);
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const level = (h[1] ?? "#").length;
      blocks.push(<div className={`md-h md-h${level}`} key={key++}>{inline(h[2] ?? "", `h${key}`)}</div>);
      i++;
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i] ?? "")) {
        const item = (lines[i] ?? "").replace(/^\s*([-*]|\d+\.)\s+/, "");
        items.push(<li key={items.length}>{inline(item, `li${key}-${items.length}`)}</li>);
        i++;
      }
      blocks.push(<ul className="md-list" key={key++}>{items}</ul>);
      continue;
    }
    if (line.trim() === "") {
      i++;
      continue;
    }
    const buf: string[] = [line];
    i++;
    while (i < lines.length && (lines[i] ?? "").trim() !== "" && !/^(#{1,4}\s|```|\s*([-*]|\d+\.)\s)/.test(lines[i] ?? "")) {
      buf.push(lines[i++] ?? "");
    }
    blocks.push(<p className="md-p" key={key++}>{inline(buf.join(" "), `p${key}`)}</p>);
  }
  return <div className="md">{blocks}</div>;
}
