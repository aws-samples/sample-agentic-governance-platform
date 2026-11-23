import McpServerCatalog from './McpServerCatalog';

export default function ToolsAndMcp() {
  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <h1 className="text-2xl font-semibold text-slate-900">Tools &amp; MCP</h1>
        <p className="text-sm text-slate-500 mt-1 mb-6">Everything agents consume — governed once, independently, because a shared MCP server is a fleet-wide supply-chain risk.</p>
        <McpServerCatalog />
      </div>
    </div>
  );
}
