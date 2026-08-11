import { Connection, WorkflowClient, type WorkflowHandle } from "@temporalio/client";
import { env } from "../config/env.js";

/**
 * Thin wrapper around the Temporal Node client, scoped to the itinerary
 * workflow contract defined in src/smi_agent/activities/itinerary_workflow.py:
 *
 *   @workflow.signal confirm()
 *   @workflow.signal reject()
 *   @workflow.signal request_changes(edit: ItineraryEditRequest)
 *   @workflow.signal rate_segment(rating: SegmentRating)
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
  userId: string;
  rawGoal: string;
}

/**
 * Starts (or, if planId was already submitted, rejoins) an itinerary workflow
 * run. Only raw_goal is required beyond identity — origin/destination/dates
 * are left unset, so ItineraryWorkflow.run() resolves them itself from
 * raw_goal via parse_trip_intent_activity before dispatching any search
 * (see the "Step 0" comment in itinerary_workflow.py).
 */
export async function startItineraryWorkflow(
  input: StartItineraryInput
): Promise<WorkflowHandle> {
  const c = await getClient();
  return c.start("ItineraryWorkflow", {
    workflowId: input.planId,
    taskQueue: env.taskQueue,
    // Matches ItineraryWorkflowInput's field names in itinerary_workflow.py.
    // Extra keys not present on that dataclass (e.g. a stray `constraints`)
    // make Temporal's payload converter fail the whole activation with a
    // TypeError, so keep this in lockstep with the dataclass — no more, no less.
    args: [
      {
        plan_id: input.planId,
        tenant_id: input.tenantId,
        user_id: input.userId,
        raw_goal: input.rawGoal,
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

export interface SegmentRatingInput {
  section: string;
  candidateId: string;
  rating: number;
}

/**
 * A 1-5 star rating for a specific candidate — feeds
 * providers/ranking/bandit.py::reward_from_rating instead of the coarse
 * accept/reject-only reward. Doesn't alter the itinerary; purely a
 * preference signal, unlike request_changes.
 */
export async function signalRateSegment(
  planId: string,
  rating: SegmentRatingInput
): Promise<void> {
  const handle = await handleFor(planId);
  // itinerary_workflow.py's rate_segment signal takes one SegmentRating
  // argument; field names are translated snake_case for the Python side.
  await handle.signal("rate_segment", {
    section: rating.section,
    candidate_id: rating.candidateId,
    rating: rating.rating,
  });
}
