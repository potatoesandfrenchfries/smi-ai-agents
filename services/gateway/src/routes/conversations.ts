import { Readable } from "node:stream";
import { Router } from "express";
import { authHeadersFor, requireUserId } from "../middleware/auth.js";
import { env } from "../config/env.js";

/**
 * Proxies to the existing FastAPI conversation service
 * (src/smi_agent/api/app.py, `/api/v1/conversations*`). Conversation
 * persistence, ceiling tracking, LangGraph orchestration, and guardrails
 * already live there — this router's job is only to give the browser one
 * origin to talk to and to keep the X-Auth-* header contract identical on
 * both sides of the gateway, per FR-INT-4 ("identical Trip object" from every
 * surface applies just as much to the conversation surface: hosted UI and
 * REST must see the same conversation, not a reimplementation of it).
 */

export const conversationsRouter = Router();
conversationsRouter.use(requireUserId);

const upstream = (path: string) => `${env.conversationApiUrl}${path}`;

function forwardHeaders(req: import("express").Request): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...authHeadersFor(req.auth!),
  };
  return headers;
}

conversationsRouter.post("/", async (req, res) => {
  const upstreamRes = await fetch(upstream("/api/v1/conversations"), {
    method: "POST",
    headers: forwardHeaders(req),
    body: JSON.stringify(req.body),
  });
  const body = await upstreamRes.json().catch(() => ({}));
  res.status(upstreamRes.status).json(body);
});

conversationsRouter.get("/", async (req, res) => {
  const qs = new URLSearchParams(req.query as Record<string, string>).toString();
  const upstreamRes = await fetch(upstream(`/api/v1/conversations${qs ? `?${qs}` : ""}`), {
    headers: forwardHeaders(req),
  });
  const body = await upstreamRes.json().catch(() => ({}));
  res.status(upstreamRes.status).json(body);
});

conversationsRouter.get("/:id", async (req, res) => {
  const upstreamRes = await fetch(upstream(`/api/v1/conversations/${req.params.id}`), {
    headers: forwardHeaders(req),
  });
  const body = await upstreamRes.json().catch(() => ({}));
  res.status(upstreamRes.status).json(body);
});

conversationsRouter.get("/:id/messages", async (req, res) => {
  const qs = new URLSearchParams(req.query as Record<string, string>).toString();
  const upstreamRes = await fetch(
    upstream(`/api/v1/conversations/${req.params.id}/messages${qs ? `?${qs}` : ""}`),
    { headers: forwardHeaders(req) }
  );
  const body = await upstreamRes.json().catch(() => ({}));
  res.status(upstreamRes.status).json(body);
});

conversationsRouter.patch("/:id", async (req, res) => {
  const upstreamRes = await fetch(upstream(`/api/v1/conversations/${req.params.id}`), {
    method: "PATCH",
    headers: forwardHeaders(req),
    body: JSON.stringify(req.body),
  });
  const body = await upstreamRes.json().catch(() => ({}));
  res.status(upstreamRes.status).json(body);
});

conversationsRouter.delete("/:id", async (req, res) => {
  const upstreamRes = await fetch(upstream(`/api/v1/conversations/${req.params.id}`), {
    method: "DELETE",
    headers: forwardHeaders(req),
  });
  res.status(upstreamRes.status).end();
});

/**
 * SSE passthrough. FastAPI streams `data: {...}\n\n` frames (token, step,
 * response, ceiling, warning, meta, done, error — see
 * src/smi_agent/conversation/sse.py); the gateway relays them byte-for-byte
 * rather than buffering, so latency is the same as talking to FastAPI directly.
 */
conversationsRouter.post("/:id/chat", async (req, res) => {
  const upstreamRes = await fetch(
    upstream(`/api/v1/conversations/${req.params.id}/chat`),
    {
      method: "POST",
      headers: forwardHeaders(req),
      body: JSON.stringify(req.body),
    }
  );

  if (!upstreamRes.ok || !upstreamRes.body) {
    const body = await upstreamRes.json().catch(() => ({ error: "Upstream chat request failed" }));
    res.status(upstreamRes.status || 502).json(body);
    return;
  }

  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });

  const nodeStream = Readable.fromWeb(upstreamRes.body as never);
  nodeStream.pipe(res);
  req.on("close", () => nodeStream.destroy());
});
