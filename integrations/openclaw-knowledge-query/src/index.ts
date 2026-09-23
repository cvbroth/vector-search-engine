/** Two model-facing capabilities; the trusted tool context supplies agent identity. */

import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { configSchema, ragContextSchema, staticParameters } from "./contracts.js";
import { queryBroker, type AccessKind } from "./unix-client.js";

const CAUTION = "Retrieval relevance is not answerability. Inspect evidence text; ACCEPT never licenses invented facts. UNCERTAIN may still be useful; REJECT with empty evidence is a valid no-evidence result.";

function capability(config: unknown, agentId: unknown, kind: AccessKind): boolean {
  if (typeof agentId !== "string" || !agentId || !config || typeof config !== "object") return false;
  const agents = (config as { agents?: unknown }).agents;
  if (!agents || typeof agents !== "object" || Array.isArray(agents) ||
      !Object.hasOwn(agents, agentId)) return false;
  const entry = (agents as Record<string, unknown>)[agentId];
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return false;
  const flags = entry as Record<string, unknown>;
  return typeof flags.private === "boolean" && typeof flags.shared === "boolean" &&
    flags[kind] === true;
}

function validateParameters(raw: unknown): { query: string; topK: number } {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("knowledge tool parameters must be an object");
  }
  const params = raw as Record<string, unknown>;
  if (Object.keys(params).some((key) => key !== "query" && key !== "top_k")) {
    throw new Error("knowledge tool received an unknown parameter");
  }
  if (typeof params.query !== "string" || !params.query.trim() ||
      Array.from(params.query).length > 4096) {
    throw new Error("query must contain 1 to 4096 Unicode characters");
  }
  const topK = params.top_k === undefined ? 5 : params.top_k;
  if (typeof topK !== "number" || !Number.isInteger(topK) || topK < 1 || topK > 10) {
    throw new Error("top_k must be an integer between 1 and 10");
  }
  return { query: params.query, topK };
}

/** The factory receives agentId from OpenClaw, never from model parameters. */
export function createKnowledgeTool(kind: AccessKind, config: unknown, agentId: unknown) {
  if (!capability(config, agentId, kind)) return null;
  const trustedAgentId = agentId as string;
  const isPrivate = kind === "private";
  return {
    name: isPrivate ? "knowledge_private" : "knowledge_shared",
    label: isPrivate ? "Private Knowledge" : "Shared Knowledge",
    description: `${isPrivate ? "Query this agent's authorized private NAS knowledge." : "Query the authorized family-shared NAS knowledge."} ${CAUTION}`,
    parameters: staticParameters,
    outputSchema: ragContextSchema,
    async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
      const { query, topK } = validateParameters(rawParams);
      const result = await queryBroker({ kind, agentId: trustedAgentId, query, topK, signal });
      return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: result };
    },
  };
}

export default defineToolPlugin({
  id: "local-knowledge-query",
  name: "Local Knowledge Query",
  description: "Read-only private and family-shared NAS knowledge tools via the local policy broker.",
  configSchema,
  tools: (tool) => (["private", "shared"] as const).map((kind) => tool({
    name: kind === "private" ? "knowledge_private" : "knowledge_shared",
    label: kind === "private" ? "Private Knowledge" : "Shared Knowledge",
    description: `${kind === "private" ? "Query this agent's authorized private NAS knowledge." : "Query the authorized family-shared NAS knowledge."} ${CAUTION}`,
    parameters: staticParameters,
    factory({ config, toolContext }) {
      return createKnowledgeTool(kind, config, toolContext.agentId);
    },
  })),
});
