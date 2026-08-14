// Custom React Flow edge for the Governance Graph (Epic 11, Task 8).
//
// Renders an agent→MCP "can_call" edge as a bezier path; when the target MCP
// gateway has Cedar enforcement on (`data.hasPolicy`), it overlays a small amber
// "policy" pill at the edge midpoint via EdgeLabelRenderer.
//
// SEMANTIC CAVEAT (research §0): Cedar policies are scoped to (user, tool) on the
// gateway MCP — they NEVER name a specific agent. So this badge means "this MCP
// gateway has Cedar enforcement on", NOT "a policy targets this agent". The
// tooltip/aria copy says exactly that and must not be reworded to imply otherwise.
//
// The badge is self-contained here (visible + hoverable). Clicking it is wired on
// the PAGE (Task 9) via React Flow's standard `onEdgeClick` — React Flow edge
// components can't cleanly receive arbitrary handler props, so the page owns the
// click → opens the detail drawer with the real (user,tool) permits. The pill sets
// `pointerEvents:'all'` (REQUIRED — the EdgeLabelRenderer layer is otherwise
// click-through) so hover/click land on it rather than passing to the canvas.

import { BaseEdge, EdgeLabelRenderer, getBezierPath } from '@xyflow/react';
import type { EdgeProps } from '@xyflow/react';
import type { FlowEdge } from './layout';

// The honest framing of the badge — quoted in the brief; do NOT reword to an
// agent-scoped claim. Reused as both the tooltip and the accessible name.
const CEDAR_ENFORCEMENT_COPY = 'This MCP gateway has Cedar enforcement on';

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
            // pointerEvents:'all' is REQUIRED — the label layer is otherwise
            // click-through and the pill would never receive hover/click.
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

// React Flow edge-type registry. can_call edges get `type:'policy'` in
// layout.toFlow(); access edges use the built-in 'default' edge (no entry needed).
export const edgeTypes = {
  policy: PolicyEdge,
};
