import { randomUUID } from "node:crypto";
import { Router } from "express";
import { z } from "zod";
import { requireUserId } from "../middleware/auth.js";
import {
  queryAvailableOptions,
  queryCurrentItinerary,
  queryEditLog,
  signalConfirm,
  signalReject,
  signalRequestChanges,
  startItineraryWorkflow,
} from "../temporal/client.js";

export const tripsRouter = Router();
tripsRouter.use(requireUserId);

const createTripSchema = z.object({
  rawGoal: z.string().min(1).max(4000),
  constraints: z.record(z.unknown()).optional(),
});

/**
 * Starts a new itinerary plan (PRD §5 stages 1-4: intent -> intake -> dispatch).
 * The plan_id doubles as the Temporal workflow ID, so resubmitting the same
 * planId rejoins the existing run instead of double-spending (PRD §10.4).
 */
tripsRouter.post("/", async (req, res) => {
  const parsed = createTripSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Invalid request body", details: parsed.error.flatten() });
    return;
  }
  if (!req.auth?.tenantId) {
    res.status(400).json({ error: "X-Auth-Tenant-Id header is required to start a plan" });
    return;
  }

  const planId = `plan-${randomUUID()}`;
  try {
    await startItineraryWorkflow({
      planId,
      tenantId: req.auth.tenantId,
      rawGoal: parsed.data.rawGoal,
      constraints: parsed.data.constraints,
    });
    res.status(202).json({ planId, status: "planning" });
  } catch (err) {
    req.log.error({ err, planId }, "failed to start itinerary workflow");
    res.status(502).json({ error: "Could not start planning workflow" });
  }
});

/** FR-PRS-3: ranked options with assumptions, price, and provenance visible to the reviewer. */
tripsRouter.get("/:planId", async (req, res) => {
  try {
    const itinerary = await queryCurrentItinerary(req.params.planId);
    if (itinerary === null || itinerary === undefined) {
      res.status(404).json({ error: `No itinerary yet for plan ${req.params.planId}` });
      return;
    }
    res.json({ planId: req.params.planId, itinerary });
  } catch (err) {
    req.log.warn({ err, planId: req.params.planId }, "current_itinerary query failed");
    res.status(404).json({ error: `Plan ${req.params.planId} not found` });
  }
});

/** Alternative candidates the traveler can swap to via request_changes. */
tripsRouter.get("/:planId/options", async (req, res) => {
  try {
    const options = await queryAvailableOptions(req.params.planId);
    res.json({ planId: req.params.planId, options });
  } catch (err) {
    req.log.warn({ err, planId: req.params.planId }, "available_options query failed");
    res.status(404).json({ error: `Plan ${req.params.planId} not found` });
  }
});

/** FR-ORC-6: the HITL edit history for this plan. */
tripsRouter.get("/:planId/edit-log", async (req, res) => {
  try {
    const editLog = await queryEditLog(req.params.planId);
    res.json({ planId: req.params.planId, editLog });
  } catch (err) {
    req.log.warn({ err, planId: req.params.planId }, "edit_log query failed");
    res.status(404).json({ error: `Plan ${req.params.planId} not found` });
  }
});

/** FR-GAT-3 / FR-PRS-2: the HITL gate. No booking handoff happens without this. */
tripsRouter.post("/:planId/confirm", async (req, res) => {
  try {
    await signalConfirm(req.params.planId);
    res.status(202).json({ planId: req.params.planId, status: "confirm_requested" });
  } catch (err) {
    req.log.error({ err, planId: req.params.planId }, "confirm signal failed");
    res.status(502).json({ error: "Could not deliver confirmation" });
  }
});

tripsRouter.post("/:planId/reject", async (req, res) => {
  try {
    await signalReject(req.params.planId);
    res.status(202).json({ planId: req.params.planId, status: "reject_requested" });
  } catch (err) {
    req.log.error({ err, planId: req.params.planId }, "reject signal failed");
    res.status(502).json({ error: "Could not deliver rejection" });
  }
});

const changesSchema = z.object({
  section: z.string().min(1),
  candidateId: z.string().min(1),
  note: z.string().max(2000).optional(),
});

/** A HITL edit re-run: swap one section's selection and let the graph re-plan around it. */
tripsRouter.post("/:planId/changes", async (req, res) => {
  const parsed = changesSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Invalid edit request", details: parsed.error.flatten() });
    return;
  }
  try {
    await signalRequestChanges(req.params.planId, parsed.data);
    res.status(202).json({ planId: req.params.planId, status: "changes_requested" });
  } catch (err) {
    req.log.error({ err, planId: req.params.planId }, "request_changes signal failed");
    res.status(502).json({ error: "Could not deliver the requested change" });
  }
});
