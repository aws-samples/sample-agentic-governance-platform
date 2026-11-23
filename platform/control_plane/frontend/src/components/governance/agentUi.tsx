import type { ReactElement } from 'react';
import { initialsFor } from '../shared/initials';

const AVATAR_PALETTE: string[] = [
  'bg-blue-100 text-blue-700',
  'bg-emerald-100 text-emerald-700',
  'bg-violet-100 text-violet-700',
  'bg-amber-100 text-amber-700',
  'bg-rose-100 text-rose-700',
  'bg-cyan-100 text-cyan-700',
  'bg-indigo-100 text-indigo-700',
  'bg-teal-100 text-teal-700',
];

const AVATAR_SIZE: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'h-7 w-7 text-xs',
  md: 'h-9 w-9 text-sm',
  lg: 'h-12 w-12 text-base',
};

function hashIndex(name: string, modulo: number): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  }
  return hash % modulo;
}

export function AgentAvatar({
  name,
  size = 'md',
}: {
  name: string;
  size?: 'sm' | 'md' | 'lg';
}): ReactElement {
  const base = `${AVATAR_SIZE[size]} rounded-lg font-semibold flex items-center justify-center shrink-0`;
  const trimmed = (name ?? '').trim();
  if (!trimmed) {
    return <div aria-hidden="true" className={`${base} bg-slate-100 text-slate-400`}>?</div>;
  }
  const color = AVATAR_PALETTE[hashIndex(trimmed, AVATAR_PALETTE.length)];
  return (
    <div aria-hidden="true" className={`${base} ${color}`}>
      {initialsFor(trimmed)}
    </div>
  );
}
