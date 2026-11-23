import type { TooltipProps } from 'recharts';

export const CHART_COLORS: readonly string[] = [
  '#3b82f6',
  '#10b981',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#14b8a6',
  '#6366f1',
];

export const tooltipStyle = {
  background: 'rgba(255,255,255,0.98)',
  border: '1px solid #e2e8f0',
  borderRadius: 8,
  fontSize: 12,
  color: '#0f172a',
  boxShadow: '0 4px 12px rgba(15,23,42,0.08)',
} satisfies NonNullable<TooltipProps['contentStyle']>;
