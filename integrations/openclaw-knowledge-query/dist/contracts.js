/** Stable request and response contracts shared by the tool and Unix client. */
import { Type } from "typebox";
export const configSchema = Type.Object({
    agents: Type.Record(Type.String(), Type.Object({
        private: Type.Boolean(),
        shared: Type.Boolean(),
    }, { additionalProperties: false })),
    imports: Type.Optional(Type.Record(Type.String(), Type.Object({
        private: Type.Boolean(),
        shared: Type.Boolean(),
    }, { additionalProperties: false }))),
}, { additionalProperties: false });
const scoreSchema = Type.Union([Type.Number(), Type.Null()]);
const pageSchema = Type.Union([Type.Integer({ minimum: 1 }), Type.Null()]);
export const evidenceSchema = Type.Object({
    rank: Type.Integer({ minimum: 1 }),
    fused_score: Type.Number(),
    semantic_score: scoreSchema,
    semantic_distance: scoreSchema,
    lexical_match: Type.Boolean(),
    lexical_score: scoreSchema,
    relevance_decision: Type.Union([Type.Literal("ACCEPT"), Type.Literal("UNCERTAIN")]),
    filename: Type.String(),
    page: pageSchema,
    chunk_index: Type.Integer({ minimum: 0 }),
    text: Type.String(),
}, { additionalProperties: false });
/** Broker response presented to the model, excluding internal routing. */
export const modelContextSchema = Type.Object({
    schema_version: Type.Literal("1.0"),
    query: Type.String(),
    retrieval_status: Type.Union([
        Type.Literal("ACCEPT"),
        Type.Literal("UNCERTAIN"),
        Type.Literal("REJECT"),
    ]),
    evidence_count: Type.Integer({ minimum: 0, maximum: 10 }),
    evidence: Type.Array(evidenceSchema, { maxItems: 10 }),
}, { additionalProperties: false });
export const staticParameters = Type.Object({
    query: Type.String({ minLength: 1, maxLength: 4096 }),
    top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })),
}, { additionalProperties: false });
export const importParameters = Type.Object({}, { additionalProperties: false });
export const importResultSchema = Type.Object({
    status: Type.Union([
        Type.Literal("QUEUED"), Type.Literal("NO_ATTACHMENT"),
        Type.Literal("SELECTION_REQUIRED"), Type.Literal("UNSUPPORTED_TYPE"),
        Type.Literal("ATTACHMENT_CHANGED"), Type.Literal("TOO_LARGE"),
        Type.Literal("ALREADY_QUEUED"), Type.Literal("BROKER_ERROR"),
    ]),
    filename: Type.Optional(Type.String()),
    content_type: Type.Optional(Type.String()),
    size_bytes: Type.Optional(Type.Integer({ minimum: 0 })),
    sha256_prefix: Type.Optional(Type.String()),
    access_kind: Type.Optional(Type.Union([Type.Literal("private"), Type.Literal("shared")])),
}, { additionalProperties: false });
