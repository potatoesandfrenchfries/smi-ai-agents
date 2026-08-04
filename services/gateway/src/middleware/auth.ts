import type { NextFunction, Request, Response } from "express";

/**
 * Mirrors src/smi_agent/api/app.py's `_require_user_id` / `_extract_tenant_id`:
 * tenancy is derived only from the authenticated connection's headers, never
 * from a request body or query string (Technical Design Reference §3.3,
 * §11.2 — "Pass tenant ID in request body: Spoofing risk. Derive from
 * authenticated header only"). This gateway sits in front of the identity
 * provider's own auth (PRD §9 step 5); in production the reverse proxy /
 * authorizer function attaches these headers after verifying the caller's
 * token. Here we only enforce that they are present and well-formed.
 */

export interface AuthContext {
  userId: string;
  tenantId: string | null;
}

declare module "express-serve-static-core" {
  interface Request {
    auth?: AuthContext;
  }
}

export function requireUserId(req: Request, res: Response, next: NextFunction): void {
  const userId = req.header("X-Auth-User-Id");
  if (!userId) {
    res.status(401).json({ error: "X-Auth-User-Id header is required" });
    return;
  }
  const tenantId = req.header("X-Auth-Tenant-Id") ?? null;
  req.auth = { userId, tenantId };
  next();
}

/** Forwards the same identity headers the gateway received on to the Python API. */
export function authHeadersFor(auth: AuthContext): Record<string, string> {
  const headers: Record<string, string> = { "X-Auth-User-Id": auth.userId };
  if (auth.tenantId) headers["X-Auth-Tenant-Id"] = auth.tenantId;
  return headers;
}
