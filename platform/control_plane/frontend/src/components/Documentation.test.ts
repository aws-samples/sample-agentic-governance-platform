import { describe, expect, it } from 'vitest';

import { stripComments } from '../rawSource';
import documentationRaw from './Documentation.tsx?raw';

const src = stripComments(documentationRaw);

describe('Documentation — the raw-HTML sink is fed only from the static docs source', () => {
  it('renders through dangerouslySetInnerHTML exactly once, and only renderMarkdown(content) reaches it', () => {
    expect(src.match(/dangerouslySetInnerHTML/g)).toHaveLength(1);
    expect(src).toMatch(/dangerouslySetInnerHTML=\{\{ __html: renderMarkdown\(content\) \}\}/);
  });

  it('content is the docs-tree section the active id selects, never a URL parameter or a response body', () => {
    expect(src).toMatch(/const content = findContent\(docs, activeId\) \|\| '';/);
    expect(src).toMatch(/const activeId = manual \?\? section \?\? 'overview';/);
  });
});
