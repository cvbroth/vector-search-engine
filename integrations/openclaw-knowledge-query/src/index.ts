/** Agent-scoped OpenClaw tool: authorization first, then fixed Unix transport. */

import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { Type } from "typebox";
import {
  configSchema,
  parametersForScopes,
  ragContextSchema,
  staticParameters,
  type Scope,
} from "./contracts.js";
import { queryKnowledgeBase } from "./unix-client.js";

const TOOL_DESCRIPTION = [
  "Query the local NAS knowledge base for evidence. retrieval_status describes retrieval relevance, not final answerability.",
  "ACCEPT does not prove the evidence answers the question; inspect the actual text before making factual claims.",
  "UNCERTAIN evidence still requires your own answerability check.",
  "REJECT with empty evidence means retrieval found no sufficiently relevant evidence.",
  "Never generate facts solely because retrieval_status is ACCEPT.",
].join(" ");

type KnowledgeParams = { query: string; scopes: Scope[]; topK: number };

function isScope(value: unknown): value is Scope {
  return value === "chen" || value === "family";
}

function allowedForAgent(config: unknown, agentId: unknown): Scope[] | null {
  if (!agentId || typeof agentId !== "string" || !config || typeof config !== "object") {
    return null;
  }
  const agentScopes = (config as { agentScopes?: unknown }).agentScopes;
  if (!agentScopes || typeof agentScopes !== "object" || Array.isArray(agentScopes) ||
      !Object.hasOwn(agentScopes, agentId)) {
    return null;
  }
  const values = (agentScopes as Record<string, unknown>)[agentId];
  if (!Array.isArray(values) || values.length === 0 || values.length > 2 ||
      values.some((value) => !isScope(value)) || new Set(values).size !== values.length) {
    return null;
  }
  return [...values] as Scope[];
}

function validateParameters(raw: unknown, allowed: readonly Scope[]): KnowledgeParams {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("knowledge_query parameters must be an object");
  }
  const params = raw as Record<string, unknown>;
  if (Object.keys(params).some((key) => !["query", "scopes", "top_k"].includes(key))) {
    throw new Error("knowledge_query received an unknown parameter");
  }
  if (typeof params.query !== "string" || !params.query.trim() ||
      Array.from(params.query).length > 4096) {
    throw new Error("query must contain 1 to 4096 Unicode characters");
  }
  if (!Array.isArray(params.scopes) || params.scopes.length === 0 ||
      params.scopes.some((scope) => !isScope(scope)) ||
      new Set(params.scopes).size !== params.scopes.length) {
    throw new Error("scopes must be a non-empty unique subset of chen and family");
  }
  if (params.scopes.some((scope: Scope) => !allowed.includes(scope))) {
    throw new Error("requested scope is not authorized for this agent");
  }
  const topK = params.top_k === undefined ? 5 : params.top_k;
  if (typeof topK !== "number" || !Number.isInteger(topK) || topK < 1 || topK > 10) {
    throw new Error("top_k must be an integer between 1 and 10");
  }
  return { query: params.query, scopes: [...params.scopes] as Scope[], topK };
}

/** Exported for unit tests; the actual factory below supplies trusted agentId. */
export function createKnowledgeTool(config: unknown, agentId: unknown) {
  const allowed = allowedForAgent(config, agentId);
  if (!allowed) return null;
  return {
    name: "knowledge_query",
    label: "Knowledge Query",
    description: TOOL_DESCRIPTION,
    parameters: parametersForScopes(allowed),
    outputSchema: ragContextSchema,
    async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
      // Recheck authorization at execution time; a model-facing schema is not a boundary.
      const params = validateParameters(rawParams, allowed);
      const result = await queryKnowledgeBase({ ...params, signal });
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
        details: result,
      };
    },
  };
}

export default defineToolPlugin({
  id: "local-knowledge-query",
  name: "Local Knowledge Query",
  description: "Agent-scoped, read-only access to the local NAS RAG Context service.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "knowledge_query",
      label: "Knowledge Query",
      description: TOOL_DESCRIPTION,
      parameters: staticParameters,
      factory({ config, toolContext }) {
        return createKnowledgeTool(config, toolContext.agentId);
      },
    }),
  ],
});
