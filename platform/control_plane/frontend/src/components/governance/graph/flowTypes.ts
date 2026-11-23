import type { ComponentType } from 'react';
import type { EdgeProps, NodeProps } from '@xyflow/react';
import type { GraphNode } from '../../../api/client';
import { AgentNode, GroupNode, McpNode, UserNode } from './graphNodes';
import type { FlowEdge, FlowNode } from './layout';
import { PolicyEdge } from './PolicyEdge';

export const nodeTypes = {
  user: UserNode,
  group: GroupNode,
  agent: AgentNode,
  mcp: McpNode,
} satisfies Record<GraphNode['type'], ComponentType<NodeProps<FlowNode>>>;

export const edgeTypes = {
  policy: PolicyEdge,
} satisfies Record<'policy', ComponentType<EdgeProps<FlowEdge>>>;
