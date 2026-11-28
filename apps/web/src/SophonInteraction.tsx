type Reason = string | null;

interface InteractionBase {
  interaction_id: string;
  voice_session_id: string;
  input_item_id: string | null;
  reason_code: Reason;
}

export interface InputObservation extends InteractionBase {
  kind: "input";
  input_item_id: string;
  status:
    | "awaiting_sophon"
    | "delegation_received"
    | "sophon_replied"
    | "no_reply_or_delegation"
    | "attribution_uncertain"
    | "observation_interrupted";
  observation_complete: boolean;
  candidate_reply: boolean;
}

export interface DelegationObservation extends InteractionBase {
  kind: "delegation";
  delegation_id: string | null;
  status: "received" | "processing" | "finished" | "interrupted";
  native_event: Record<string, unknown> | null;
  structure: "valid" | "invalid" | "unavailable";
  field_issues: {
    path: string;
    expected: string;
    received: string;
    length: number | null;
  }[];
  task_chars: number | null;
  redacted_fields: string[];
  omitted_fields: number;
  duplicates: number;
  details_restricted: boolean;
  intent: {
    initial_route: string | null;
    route: string;
    parse_method: string;
    router_model: string | null;
    selected_tool: string | null;
    parameters: Record<string, string | null> | null;
    parameters_redacted: boolean;
    reason_code: Reason;
    policy_reason: Reason;
  } | null;
  turn_id: string | null;
  request_revision: number | null;
  tool_executions: {
    operation: "adapter" | "cuekb_query" | "cuekb_validate";
    tool: string;
    service: string;
    mode: string;
    entered: boolean;
    status: "started" | "completed" | "blocked" | "failed" | "canceled";
    result_status: string | null;
    reason_code: Reason;
    duration_ms: number | null;
  }[];
  result_status: string | null;
  result_reason: Reason;
  delivery: {
    kind: "tool_result" | "disposition";
    status: "queued" | "gateway_received" | "receipt_unknown" | "discarded";
    reason_code: Reason;
  } | null;
}

export interface UnassignedObservation extends InteractionBase {
  kind: "unassigned";
  status: "attribution_uncertain" | "protocol_error" | "diagnostics_incomplete";
}

export type SophonObservation =
  InputObservation | DelegationObservation | UnassignedObservation;
export const sophonInteractionEvent = "portal.sophon_interaction.updated";
export const sophonInteractionRecord = "sophon_interaction";

export function restrictDelegation(item: SophonObservation): SophonObservation {
  if (item.kind !== "delegation") return item;
  return {
    ...item,
    native_event: null,
    details_restricted: true,
    intent: item.intent
      ? { ...item.intent, parameters: null, parameters_redacted: true }
      : null,
  };
}

const inputLabels: Record<InputObservation["status"], string> = {
  awaiting_sophon: "Awaiting Sophon",
  delegation_received: "Delegation received",
  sophon_replied: "Sophon replied · no delegation observed",
  no_reply_or_delegation: "No reply or delegation observed",
  attribution_uncertain: "Attribution uncertain",
  observation_interrupted: "Observation interrupted",
};
const operationLabels = {
  adapter: "Adapter",
  cuekb_query: "Evidence query",
  cuekb_validate: "Evidence validation",
};
const toolLabels = {
  started: "Running",
  completed: "Completed",
  blocked: "Not called",
  failed: "Failed",
  canceled: "Canceled",
};
const deliveryLabels = {
  queued: "Queued for Sophon",
  gateway_received: "Gateway acknowledged receipt",
  receipt_unknown: "Gateway receipt unknown",
  discarded: "Discarded",
};
const resultLabels: Record<string, string> = {
  answered: "Usable result",
  insufficient_evidence: "No usable result",
  needs_clarification: "Clarification needed",
  failed: "Failed",
};

function DelegationDetails({ item }: { item: DelegationObservation }) {
  const intent = item.intent;
  const pending = item.status === "received" || item.status === "processing";
  const interrupted =
    item.status === "interrupted" &&
    item.reason_code !== "SDK_DELEGATION_NOT_DISPATCHED";
  let label = "Delegation details";
  if (item.structure === "invalid") label = "Invalid delegation structure";
  else if (item.status === "interrupted") label = "Delegation interrupted";
  else if (
    item.status === "finished" &&
    !item.tool_executions.some((run) => run.entered)
  )
    label = "Delegation · tool not called";

  return (
    <details className="sophon-delegation">
      <summary>
        <span>{label}</span>
        <code>{item.delegation_id || item.interaction_id}</code>
      </summary>
      <dl>
        <dt>Sophon payload</dt>
        <dd>
          {item.native_event ? (
            <pre>{JSON.stringify(item.native_event, null, 2)}</pre>
          ) : item.details_restricted ? (
            "Payload access unavailable"
          ) : (
            "Original event unavailable"
          )}
          {!!item.field_issues.length && (
            <ul>
              {item.field_issues.map((issue) => (
                <li key={issue.path}>
                  <code>{issue.path}</code> expects {issue.expected}. Received{" "}
                  {issue.received}
                  {issue.length !== null ? ` (length ${issue.length})` : ""}.
                </li>
              ))}
            </ul>
          )}
          {!!item.redacted_fields.length && (
            <small>
              Values withheld for {item.redacted_fields.join(", ")}.
            </small>
          )}
          {!!item.omitted_fields && (
            <small>{item.omitted_fields} additional fields omitted.</small>
          )}
          {!!item.duplicates && (
            <small>Repeated events observed {item.duplicates}.</small>
          )}
        </dd>
        <dt>Interpreted intent</dt>
        <dd>
          {intent ? (
            <>
              <strong>
                {intent.initial_route && intent.initial_route !== intent.route
                  ? `${intent.initial_route} → ${intent.route}`
                  : intent.route}
              </strong>
              <small>
                {[
                  intent.parse_method,
                  intent.router_model,
                  intent.selected_tool &&
                    `Selected tool ${intent.selected_tool}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </small>
              {intent.parameters && (
                <pre>{JSON.stringify(intent.parameters, null, 2)}</pre>
              )}
              {intent.parameters_redacted && (
                <small>Parameters withheld.</small>
              )}
              {(intent.policy_reason || intent.reason_code) && (
                <code>{intent.policy_reason || intent.reason_code}</code>
              )}
            </>
          ) : pending ? (
            "Awaiting intent analysis"
          ) : interrupted ? (
            "Intent outcome unconfirmed"
          ) : (
            "Intent analysis did not run"
          )}
        </dd>
        <dt>Tool execution</dt>
        <dd>
          {item.tool_executions.length ? (
            <ul>
              {item.tool_executions.map((run) => (
                <li key={run.operation}>
                  {run.service} · <code>{run.tool}</code> ·{" "}
                  {operationLabels[run.operation]} · {run.mode}
                  <small>
                    {interrupted && run.status === "started"
                      ? "Started · outcome unconfirmed"
                      : run.entered
                        ? toolLabels[run.status]
                        : toolLabels.blocked}
                    {run.result_status &&
                      ` · ${run.result_status.replaceAll("_", " ")}`}
                    {run.reason_code && ` · ${run.reason_code}`}
                    {run.duration_ms !== null && ` · ${run.duration_ms} ms`}
                  </small>
                </li>
              ))}
            </ul>
          ) : pending ? (
            "Awaiting tool execution"
          ) : interrupted ? (
            "Execution outcome unconfirmed"
          ) : (
            "Tool not called"
          )}
          {item.result_status && (
            <small>
              {resultLabels[item.result_status] || item.result_status}
            </small>
          )}
          {item.result_reason && <code>{item.result_reason}</code>}
        </dd>
        <dt>Delivery</dt>
        <dd>
          {item.delivery ? (
            <>
              {item.delivery.kind === "tool_result"
                ? "Tool result"
                : "Disposition"}{" "}
              · {deliveryLabels[item.delivery.status]}
              {item.delivery.reason_code &&
                item.delivery.reason_code !== item.reason_code && (
                  <small>
                    <code>{item.delivery.reason_code}</code>
                  </small>
                )}
            </>
          ) : pending ? (
            "Waiting"
          ) : (
            "No delivery recorded"
          )}
          {item.reason_code && (
            <small>
              <code>{item.reason_code}</code>
            </small>
          )}
        </dd>
      </dl>
    </details>
  );
}

export function SophonInteraction({
  input,
  delegations,
}: {
  input?: InputObservation;
  delegations: DelegationObservation[];
}) {
  return (
    <div className="sophon-interaction">
      <div className="sophon-status" aria-live="polite">
        {inputLabels[input?.status || "delegation_received"]}
        {input?.status === "delegation_received" &&
          input.reason_code &&
          " · Observation interrupted"}
      </div>
      {input?.candidate_reply && <small>Candidate reply observed.</small>}
      {delegations.map((item) => (
        <DelegationDetails key={item.interaction_id} item={item} />
      ))}
    </div>
  );
}

export function SophonUnassigned({
  items,
}: {
  items: (DelegationObservation | UnassignedObservation)[];
}) {
  return (
    <aside
      className="sophon-interaction sophon-unassigned"
      aria-label="Unassigned Sophon events"
    >
      <strong>Unassigned events</strong>
      {items.map((item) =>
        item.kind === "delegation" ? (
          <DelegationDetails key={item.interaction_id} item={item} />
        ) : (
          <small key={item.interaction_id}>
            {item.status === "diagnostics_incomplete"
              ? "Some interaction updates may be missing."
              : item.status === "protocol_error"
                ? "Invalid Sophon event"
                : "Reply attribution uncertain"}
          </small>
        ),
      )}
    </aside>
  );
}
