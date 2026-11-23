import { createElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

const portal = vi.hoisted(() => ({ targets: [] as unknown[] }));
vi.mock('react-dom', async (orig) => ({
  ...(await orig<typeof import('react-dom')>()),
  createPortal: (node: ReactNode, target: unknown) => {
    portal.targets.push(target);
    return node;
  },
}));

import ConfirmDialog from './ConfirmDialog';

const props = { title: 'Restore agent?', message: 'Lifts the containment.', onConfirm: () => {}, onCancel: () => {} };

afterEach(() => {
  portal.targets.length = 0;
  vi.unstubAllGlobals();
});

describe('ConfirmDialog', () => {
  it('renders into document.body: a parent with backdrop-blur would otherwise be the containing block of the fixed dialog and centre it off-screen', () => {
    const body = { id: 'body' };
    vi.stubGlobal('document', { body });
    const html = renderToStaticMarkup(createElement(ConfirmDialog, { ...props, open: true }));
    expect(portal.targets).toEqual([body]);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('Restore agent?');
  });

  it('renders nothing and touches no document when closed', () => {
    expect(renderToStaticMarkup(createElement(ConfirmDialog, { ...props, open: false }))).toBe('');
    expect(portal.targets).toEqual([]);
  });
});
