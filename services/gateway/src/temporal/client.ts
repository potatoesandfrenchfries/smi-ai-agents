import { Connection, WorkflowClient, type WorkflowHandle } from "@temporalio/client";
import { env } from "../config/env.js";

/**
 * Thin wrapper around the Temporal Node client, scoped to the itinerary
 * workflow contract defined in src/smi_agent/activities/itinerary_workflow.py:
 *
 *   @workflow.signal confirm()
 *   @workflow.signal reject()
 *   @workflow.signal request_changes(edit: ItineraryEditRequest)
 *   @workflow.query  current_itinerary() -> ItineraryResult | None
 *   @workflow.query  available_options() -> dict[str, list[dict]]
 *   @workflow.query  edit_log() -> list[str]
 *
 * The workflow itself is authored in Python and runs on the shared worker
 * (`make worker`). Temporal's wire protocol is language-neutral, so this
 * client signals and queries that same running workflow by workflow ID —
 * it never needs the Python class, only its name and the string signal/query
 * names above, which is the intended cross-language integration point.
 */

let connectionPromise: Promise<Connection> | null = null;
let client: WorkflowClient | null = null;

async function getClient(): Promise<WorkflowClient> {
  if (client) return client;
  connectionPromise ??= Connection.connect({ address: env.temporalAddress });
  const connection = await connectionPromise;
  client = new WorkflowClient({ connection, namespace: env.temporalNamespace });
  return client;
}

export interface StartItineraryInput {
  planId: string;
  tenantId: string;
  rawGoal: string;
  constraints?: Record<string, unknown>;
}

/** Starts (or, if planId was already submitted, rejoins) an itinerary workflow run. */
export async function startItineraryWorkflow(
  input: StartItineraryInput
): Promise<WorkflowHandle> {
  const c = await getClient();
  return c.start("ItineraryWorkflow", {
    workflowId: input.planId,
    taskQueue: env.taskQueue,
    // Matches ItineraryWorkflowInput's field names in itinerary_workflow.py.
    args: [
      {
        plan_id: input.planId,
        tenant_id: input.tenantId,
        raw_goal: input.rawGoal,
        constraints: input.constraints ?? {},
      },
    ],
  });
}

function handleFor(planId: string): Promise<WorkflowHandle> {
  return getClient().then((c) => c.getHandle(planId));
}

export async function queryCurrentItinerary(planId: string): Promise<unknown> {
  const handle = await handleFor(planId);
  return handle.query("current_itinerary");
}

export async function queryAvailableOptions(planId: string): Promise<unknown> {
  const handle = await handleFor(planId);
  return handle.query("available_options");
}

export async function queryEditLog(planId: string): Promise<unknown> {
  const handle = await handleFor(planId);
  return handle.query("edit_log");
}

export async function signalConfirm(planId: string): Promise<void> {
  const handle = await handleFor(planId);
  await handle.signal("confirm");
}

export async function signalReject(planId: string): Promise<void> {
  const handle = await handleFor(planId);
  await handle.signal("reject");
}

export interface ItineraryEditRequest {
  section: string;
  candidateId: string;
  note?: string;
}

export async function signalRequestChanges(
  planId: string,
  edit: ItineraryEditRequest
): Promise<void> {
  const handle = await handleFor(planId);
  // itinerary_workflow.py's request_changes signal takes one ItineraryEditRequest
  // argument; field names are translated snake_case for the Python side.
  await handle.signal("request_changes", {
    section: edit.section,
    candidate_id: edit.candidateId,
    note: edit.note ?? null,
  });
}
