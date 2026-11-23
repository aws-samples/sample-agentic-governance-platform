import { parseMarkdown } from './invokeReply';
import type { Inline } from './invokeReply';

function Inlines({ inlines }: { inlines: Inline[] }) {
  return (
    <>
      {inlines.map((n, i) =>
        n.kind === 'bold' ? (
          <strong key={i} className="font-semibold text-slate-800">{n.text}</strong>
        ) : n.kind === 'code' ? (
          <code key={i} className="rounded bg-slate-100 px-1 font-mono text-xs">{n.text}</code>
        ) : (
          <span key={i}>{n.text}</span>
        ),
      )}
    </>
  );
}

export default function ReplyMarkdown({ text, className }: { text: string; className: string }) {
  return (
    <div className={className}>
      {parseMarkdown(text).map((b, i) => {
        if (b.kind === 'heading') {
          return <p key={i} className="font-semibold text-slate-800"><Inlines inlines={b.inlines} /></p>;
        }
        if (b.kind === 'code') {
          return (
            <pre key={i} className="overflow-auto rounded bg-slate-100 px-2 py-1.5 font-mono text-xs whitespace-pre-wrap">
              {b.text}
            </pre>
          );
        }
        if (b.kind === 'table') {
          return (
            <div key={i} className="overflow-x-auto">
              <table className="min-w-full border-collapse text-left text-xs">
                <thead>
                  <tr>
                    {b.header.map((h, j) => (
                      <th key={j} className="border-b border-slate-300 px-2 py-1 font-semibold text-slate-800">
                        <Inlines inlines={h} />
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.rows.map((r, j) => (
                    <tr key={j} className="border-b border-slate-200/70 last:border-0">
                      {r.map((c, k) => (
                        <td key={k} className="px-2 py-1 align-top"><Inlines inlines={c} /></td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (b.kind === 'ul' || b.kind === 'ol') {
          const List = b.kind;
          return (
            <List key={i} className={`pl-5 space-y-0.5 ${b.kind === 'ul' ? 'list-disc' : 'list-decimal'}`}>
              {b.items.map((it, j) => <li key={j}><Inlines inlines={it} /></li>)}
            </List>
          );
        }
        return (
          <p key={i}>
            {b.lines.map((l, j) => (
              <span key={j}>{j > 0 && <br />}<Inlines inlines={l} /></span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
