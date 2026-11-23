import { describe, expect, it } from 'vitest';
import html from '../index.html?raw';

describe('index.html build SHA marker', () => {
  it('has exactly one root element', () => {
    expect(html.match(/id="root"/g)).toHaveLength(1);
  });

  it('root element carries the Vite-substituted build SHA placeholder', () => {
    expect(html).toContain('<div id="root" data-build-sha="%VITE_BUILD_SHA%">');
  });
});
