/** Two model-facing capabilities; the trusted tool context supplies agent identity. */

import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { configSchema, importParameters, importResultSchema, modelContextSchema, staticParameters } from "./contracts.js";
import { AttachmentRegistry } from "./attachment-registry.js";
import { queueAttachment, type ImportOutcome } from "./import-client.js";
import { queryBroker, type AccessKind } from "./unix-client.js";

const CAUTION = "Retrieval relevance is not answerability. Inspect evidence text; ACCEPT never licenses invented facts. UNCERTAIN may still be useful; REJECT with empty evidence is a valid no-evidence result.";
const registry = new AttachmentRegistry();
const IMPORT_DESCRIPTION = "Use only after the user explicitly asks to queue this session's most recent single trusted attachment for this destination. Without matching user consent the tool returns CONSENT_REQUIRED. QUEUED does not mean indexed.";

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

function importCapability(config: unknown, agentId: unknown, kind: AccessKind): boolean {
  if (typeof agentId !== "string" || !agentId || !config || typeof config !== "object") return false;
  const imports = (config as { imports?: unknown }).imports;
  if (!imports || typeof imports !== "object" || Array.isArray(imports) ||
      !Object.hasOwn(imports, agentId)) return false;
  const entry = (imports as Record<string, unknown>)[agentId];
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return false;
  const flags = entry as Record<string, unknown>;
  return typeof flags.private === "boolean" && typeof flags.shared === "boolean" && flags[kind] === true;
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
    description: `${isPrivate ? "Query this agent's authorized private NAS knowledge." : "Query authorized household-shared NAS knowledge."} ${CAUTION}`,
    parameters: staticParameters,
    outputSchema: modelContextSchema,
    async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
      const { query, topK } = validateParameters(rawParams);
      const result = await queryBroker({ kind, agentId: trustedAgentId, query, topK, signal });
      return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: result };
    },
  };
}

export function createImportTool(
  kind: AccessKind, config: unknown, agentId: unknown, sessionKey: unknown,
  attachments: AttachmentRegistry = registry,
) {
  if (!importCapability(config, agentId, kind) || typeof sessionKey !== "string" || !sessionKey) return null;
  const trustedAgentId = agentId as string;
  const trustedSessionKey = sessionKey;
  return {
    name: kind === "private" ? "knowledge_import_private" : "knowledge_import_shared",
    label: kind === "private" ? "Import Private Knowledge" : "Import Shared Knowledge",
    description: IMPORT_DESCRIPTION,
    parameters: importParameters,
    outputSchema: importResultSchema,
    async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
      if (!rawParams || typeof rawParams !== "object" || Array.isArray(rawParams) ||
          Object.keys(rawParams).length !== 0) throw new Error("knowledge import parameters must be an empty object");
      let result: ImportOutcome;
      const ready = await attachments.waitForRegistration(trustedAgentId, trustedSessionKey);
      const selected = ready ? attachments.select(trustedAgentId, trustedSessionKey) : { status: "NO_ATTACHMENT" as const };
      if (selected.status !== "READY") {
        result = { status: selected.status };
      } else if (!attachments.hasConsent(trustedAgentId, trustedSessionKey, kind, selected.attachment)) {
        result = { status: "CONSENT_REQUIRED" };
      } else {
        const opened = await attachments.openSelected(selected.attachment);
        if (typeof opened === "string") result = { status: opened };
        else {
          const started = attachments.startIfConsented(trustedAgentId, trustedSessionKey, kind, selected.attachment);
          if (started !== "STARTED") {
            await opened.handle.close();
            result = { status: started };
          } else {
            try {
              result = await queueAttachment(opened, trustedAgentId, kind, signal);
              if (result.status === "QUEUED") attachments.queued(selected.attachment);
              else if (result.status === "TOO_LARGE") attachments.tooLarge(selected.attachment);
              else throw new Error("unexpected import broker result");
            } catch {
              // After the request starts, a timeout may mean the broker accepted it.
              // Keep SENDING to prevent accidental duplicate queueing.
              result = { status: "BROKER_ERROR" };
            }
          }
        }
      }
      return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: result };
    },
  };
}

const plugin = defineToolPlugin({
  id: "local-knowledge-query",
  name: "Local Knowledge Query",
  description: "Private and household-shared NAS query and trusted-attachment import tools.",
  configSchema,
  tools: (tool) => [
    ...(["private", "shared"] as const).map((kind) => tool({
    name: kind === "private" ? "knowledge_private" : "knowledge_shared",
    label: kind === "private" ? "Private Knowledge" : "Shared Knowledge",
    description: `${kind === "private" ? "Query this agent's authorized private NAS knowledge." : "Query authorized household-shared NAS knowledge."} ${CAUTION}`,
    parameters: staticParameters,
    factory({ config, toolContext }) {
      return createKnowledgeTool(kind, config, toolContext.agentId);
    },
    })),
    ...(["private", "shared"] as const).map((kind) => tool({
      name: kind === "private" ? "knowledge_import_private" : "knowledge_import_shared",
      label: kind === "private" ? "Import Private Knowledge" : "Import Shared Knowledge",
      description: IMPORT_DESCRIPTION,
      parameters: importParameters,
      factory({ config, toolContext }) {
        return createImportTool(kind, config, toolContext.agentId, toolContext.sessionKey);
      },
    })),
  ],
});

// defineToolPlugin supplies static metadata for build/validate. The public
// Plugin API additionally observes ordinary inbound messages before agent dispatch.
const registerTools = plugin.register;
plugin.register = (api) => {
  registerTools(api);
  api.on("message_received", async (event, context) => {
    await registry.registerMessageReceived(event, context);
  });
};

export default plugin;
