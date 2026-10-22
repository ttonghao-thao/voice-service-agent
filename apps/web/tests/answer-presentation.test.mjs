import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const exports = {};
vm.runInNewContext(ts.transpileModule(
  readFileSync(new URL("../src/answerPresentation.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } },
).outputText, { exports });
const { answerLabel, sourceNotice } = exports;

test("successful search with rejected speech is not presented as a failed search", () => {
  const answer = { status: "failed", reason_code: "VOICE_MISSING_CONDITION",
    verification_timing: "after_audio", citations: [{ citation_id: "C1" }] };
  assert.equal(answerLabel(answer), "Answer not verified");
  assert.match(sourceNotice(answer), /do not verify the spoken claims/);
  assert.equal(answerLabel({ ...answer, reason_code: "FORBIDDEN", citations: [] }), "Source access unavailable");
});

test("completed reply does not claim requested facts were found", () => {
  const answer = { status: "answered", citations: [{ citation_id: "C1" }] };
  assert.equal(answerLabel(answer), "Reply complete");
  assert.match(sourceNotice(answer), /does not mean every requested detail was found/);
  assert.equal(answerLabel({ status: "failed", reason_code: "CUEKB_AUTH_FAILED" }), "Search failed");
  assert.equal(answerLabel({ status: "failed", reason_code: "AGENT_FAILED" }), "Request failed");
});
