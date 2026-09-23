/** Stable request and response contracts shared by the tool and Unix client. */

import { Type, type Static } from "typebox";

export const scopeSchema = Type.Union([Type.Literal("chen"), Type.Literal("family")]);
export type Scope = Static<typeof scopeSchema>;

export const configSchema = Type.Object(
  {
    agentScopes: Type.Record(
      Type.String(),
      Type.Array(scopeSchema, { minItems: 1, maxItems: 2, uniqueItems: true }),
    ),
  },
  { additionalProperties: false },
);

const scoreSchema = Type.Union([Type.Number(), Type.Null()]);
const pageSchema = Type.Union([Type.Integer({ minimum: 1 }), Type.Null()]);

export const evidenceSchema = Type.Object(
  {
    scope: scopeSchema,
    rank: Type.Integer({ minimum: 1 }),
    fused_score: Type.Number(),
    semantic_score: scoreSchema,
    semantic_distance: scoreSchema,
    lexical_match: Type.Boolean(),
    lexical_score: scoreSchema,
    relevance_decision: Type.Union([Type.Literal("ACCEPT"), Type.Literal("UNCERTAIN")]),
    source_path: Type.String(),
    filename: Type.String(),
    page: pageSchema,
    chunk_index: Type.Integer({ minimum: 0 }),
    text: Type.String(),
  },
  { additionalProperties: false },
);

export const ragContextSchema = Type.Object(
  {
    schema_version: Type.Literal("1.0"),
    query: Type.String(),
    scopes: Type.Array(scopeSchema, { minItems: 1, maxItems: 2, uniqueItems: true }),
    retrieval_status: Type.Union([
      Type.Literal("ACCEPT"),
      Type.Literal("UNCERTAIN"),
      Type.Literal("REJECT"),
    ]),
    evidence_count: Type.Integer({ minimum: 0, maximum: 10 }),
    evidence: Type.Array(evidenceSchema, { maxItems: 10 }),
  },
  { additionalProperties: false },
);

export type RagContext = Static<typeof ragContextSchema>;

export const staticParameters = Type.Object(
  {
    query: Type.String({ minLength: 1, maxLength: 4096 }),
    scopes: Type.Array(scopeSchema, { minItems: 1, maxItems: 2, uniqueItems: true }),
    top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })),
  },
  { additionalProperties: false },
);

export function parametersForScopes(allowed: readonly Scope[]) {
  return Type.Object(
    {
      query: Type.String({ minLength: 1, maxLength: 4096 }),
      scopes: Type.Array(Type.String({ enum: [...allowed] }), {
        minItems: 1,
        maxItems: allowed.length,
        uniqueItems: true,
      }),
      top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })),
    },
    { additionalProperties: false },
  );
}
