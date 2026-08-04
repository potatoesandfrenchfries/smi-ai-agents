import express from "express";
import cors from "cors";
import { pinoHttp } from "pino-http";
import { env } from "./config/env.js";
import { tripsRouter } from "./routes/trips.js";
import { conversationsRouter } from "./routes/conversations.js";

const app = express();

app.use(pinoHttp({ level: env.logLevel }));
app.use(
  cors({
    origin: env.corsOrigin,
    allowedHeaders: ["Content-Type", "X-Auth-User-Id", "X-Auth-Tenant-Id", "X-Api-Key"],
  })
);
app.use(express.json({ limit: "256kb" }));

app.get("/healthz", (_req, res) => {
  res.json({ status: "ok" });
});

app.use("/api/v1/trips", tripsRouter);
app.use("/api/v1/conversations", conversationsRouter);

// eslint-disable-next-line @typescript-eslint/no-unused-vars
app.use((err: unknown, req: express.Request, res: express.Response, _next: express.NextFunction) => {
  req.log.error({ err }, "unhandled gateway error");
  res.status(500).json({ error: "Internal gateway error" });
});

app.listen(env.port, () => {
  console.log(`Smartinerary gateway listening on :${env.port}`);
  console.log(`  Conversation API upstream: ${env.conversationApiUrl}`);
  console.log(`  Temporal:                  ${env.temporalAddress} (namespace=${env.temporalNamespace}, queue=${env.taskQueue})`);
});
