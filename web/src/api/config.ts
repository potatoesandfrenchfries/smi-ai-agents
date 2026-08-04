export const GATEWAY_URL: string =
  (import.meta.env.VITE_GATEWAY_URL as string | undefined) ?? "http://localhost:4000";

/**
 * Dev-only stand-ins for the identity headers a real deployment attaches
 * after verifying the caller's token against the identity provider (PRD §9
 * step 5). The gateway and the Python API both require these — see
 * services/gateway/src/middleware/auth.ts and api/app.py's
 * `_require_user_id`. Swap for real auth wiring (OIDC/session cookie ->
 * these headers) before this ships anywhere but a laptop.
 */
export const DEV_USER_ID = "usr-demo-traveler";
export const DEV_TENANT_ID = "org-demo-acme";

export function authHeaders(): Record<string, string> {
  return {
    "X-Auth-User-Id": DEV_USER_ID,
    "X-Auth-Tenant-Id": DEV_TENANT_ID,
  };
}
