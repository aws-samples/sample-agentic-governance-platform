import { BaseEdge, EdgeLabelRenderer, getBezierPath } from '@xyflow/react';
import type { EdgeProps } from '@xyflow/react';
import type { FlowEdge } from './layout';
import { CEDAR_ENFORCEMENT_COPY } from './cedarCopy';

export function PolicyEdge({
  id,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  data,
}: EdgeProps<FlowEdge>) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  const hasPolicy = data?.hasPolicy ?? false;

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={hasPolicy ? { stroke: '#d97706', strokeWidth: 1.75 } : undefined}
      />
      {hasPolicy && (
        <EdgeLabelRenderer>
          <button
            type="button"
            // React Flow's EdgeLabelRenderer layer passes pointer events through unless the label opts back in.
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: 'all',
            }}
            title={CEDAR_ENFORCEMENT_COPY}
            aria-label={CEDAR_ENFORCEMENT_COPY}
            className="nodrag nopan inline-flex items-center gap-1 rounded-full bg-amber-100 text-amber-800 text-xs font-medium px-2 py-0.5 shadow hover:bg-amber-200 transition-colors"
          >
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-amber-500" />
            policy
          </button>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
