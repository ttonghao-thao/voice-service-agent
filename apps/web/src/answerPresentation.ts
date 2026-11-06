import type { Answer } from "./api";

export function answerLabel(answer: Answer): string {
  if (answer.reason_code === "VOICE_EVIDENCE_BUDGET" && answer.status === "insufficient_evidence") return "Limited evidence delivery";
  if (answer.reason_code === "CUEKB_PARTIAL_ANSWER") return "Partial answer";
  if (answer.reason_code === "CUEKB_CONFLICT") return "Conflicting sources";
  if (answer.status === "insufficient_evidence") return "Insufficient evidence";
  if (answer.status === "needs_clarification") return "Clarification needed";
  if (answer.status === "answered") return "Reply complete";
  if (answer.status !== "failed") return "";
  if (answer.verification_timing === "after_audio") {
    return answer.reason_code === "FORBIDDEN" || answer.reason_code === "KB_ACCESS_REVOKED"
      ? "Source access unavailable"
      : "Answer not verified";
  }
  return answer.reason_code?.startsWith("CUEKB_") || answer.reason_code?.startsWith("TOOL_")
    ? "Search failed"
    : "Request failed";
}

export function sourceNotice(answer: Answer): string {
  if (answer.reason_code === "VOICE_EVIDENCE_BUDGET" && answer.status === "insufficient_evidence") {
    return "Some complete evidence groups could not fit the voice reply. The spoken response covers only the delivered evidence.";
  }
  if (answer.status === "failed") {
    return answer.citations.length
      ? "The spoken answer did not pass verification. These retrieved sources are available for review; they do not verify the spoken claims."
      : answer.display_text;
  }
  return answer.citations.length
    ? "These are retrieved excerpts. Reply complete does not mean every requested detail was found or that every spoken claim is verified."
    : "";
}
