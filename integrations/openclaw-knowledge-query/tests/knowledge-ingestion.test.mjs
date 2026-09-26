/** Static Skill contract checks. Live Agent wording still requires model evaluation. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { parseExplicitImportConsent } from "../dist/import-consent.js";

const skillPath = fileURLToPath(new URL("../skills/knowledge-ingestion/SKILL.md", import.meta.url));
const skill = await readFile(skillPath, "utf8");

function section(start, end) {
  const afterStart = skill.split(start)[1];
  assert.ok(afterStart, `missing section ${start}`);
  return afterStart.split(end)[0];
}

test("Skill defines all seven states and only consent can lead to import", () => {
  for (const state of ["ATTACHMENT_RECEIVED", "ATTACHMENT_TRIAGED", "ATTACHMENT_DISCUSSING",
    "DURABLE_VALUE_CANDIDATE", "IMPORT_SUGGESTED", "IMPORT_CONSENTED", "IMPORT_QUEUED"]) {
    assert.match(skill, new RegExp(`\\b${state}\\b`));
  }
  assert.match(skill, /Only \*\*F → an import-tool call → G on `QUEUED`\*\* is allowed/);
  assert.match(skill, /Never call an import tool from A, B, C, D, or E/);
  assert.match(skill, /direct, explicit import request may move from A to F/);
});

test("3D guide, paper, and vague look request stay at natural first-look triage", () => {
  const triage = section("## First layer: quick attachment triage", "## Second layer:");
  assert.match(triage, /1–3 natural-language sentences/);
  assert.match(triage, /do not mention the knowledge base, saving, import authorization/);
  assert.match(triage, /do not call an import tool/);
  assert.match(triage, /do not recite filename, MIME type, size, attachment ID, staged path/);
  for (const label of ["3D-printing guide", "Research paper"]) {
    const reply = triage.match(new RegExp(`- ${label}: “([^”]+)”`))?.[1];
    assert.ok(reply, `missing ${label} first-look example`);
    assert.match(reply, /[？?]$/u);
    assert.doesNotMatch(reply, /知识库|导入|授权|保存|附件ID|路径/u);
    assert.ok(reply.split(/[。！？?]/u).filter(Boolean).length <= 3);
  }
  assert.match(triage, /“帮我看看这个”/);
  assert.match(triage, /Do not treat this vague request as a request to save it/);
});

test("substantive follow-up precedes one optional suggestion; temporary and refused files are excluded", () => {
  const discussion = section("## Second layer: discuss first, suggest at most once", "## Third layer:");
  assert.match(discussion, /technical point/);
  assert.match(discussion, /normal, thorough answer/);
  assert.match(discussion, /Finish the user's current task \*\*before\*\*/);
  assert.match(discussion, /long-term reuse value/);
  assert.match(discussion, /at the end of the answer/);
  assert.match(discussion, /do not ask again/);
  for (const phrase of ["Temporary logs", "invoices", "delivery slips", "先临时看看", "不要保存"]) {
    assert.ok(discussion.includes(phrase), phrase);
  }
});

test("generic and specific proposals plus bare assent cannot bypass the current gate", () => {
  const consent = section("## Third layer: explicit import consent", "## Trusted attachment");
  assert.match(consent, /“可以” after “要不要存进知识库？” lacks a destination and is not F/);
  assert.match(consent, /“可以” after “要不要存到你的私人知识库？” also is \*\*not F in the current plugin\*\*/);
  for (const message of ["可以", "私人", "存起来"]) {
    assert.equal(parseExplicitImportConsent(message), null);
  }
  assert.equal(parseExplicitImportConsent("把这份资料放私人知识库"), "private");
  assert.equal(parseExplicitImportConsent("把这份资料放家庭共享知识库"), "shared");
  assert.match(consent, /do not call the tool or bypass the gate/);
});
