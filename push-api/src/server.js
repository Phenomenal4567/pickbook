import "dotenv/config";
import cors from "cors";
import express from "express";
import helmet from "helmet";
import {
  buildPushPayload,
  deleteSubscriptionByEndpoint,
  findSubscriptions,
  publicVapidKey,
  sendPushNotifications,
  upsertPushSubscription,
} from "./push.js";
import { requirePushAdmin, requireSupabaseUser } from "./middleware.js";

const app = express();
const port = Number(process.env.PORT || 8080);
const appOrigin = process.env.APP_ORIGIN || "http://localhost:8000";

app.disable("x-powered-by");
app.use(helmet());
app.use(cors({
  origin: appOrigin.split(",").map(origin => origin.trim()),
  credentials: true,
}));
app.use(express.json({ limit: "32kb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "pickbook-push-api" });
});

app.get("/api/push/vapid-public-key", (_req, res) => {
  res.json({ publicKey: publicVapidKey() });
});

app.post("/api/push/subscribe", requireSupabaseUser, async (req, res, next) => {
  try {
    const { userId, subscription } = req.body || {};

    if (!userId || userId !== req.user.id) {
      return res.status(403).json({ error: "Subscription userId must match authenticated user." });
    }

    const saved = await upsertPushSubscription({
      userId,
      subscription,
      userAgent: req.get("user-agent"),
    });

    return res.status(200).json({
      status: "subscribed",
      id: saved.id,
      userId: saved.user_id,
    });
  } catch (error) {
    return next(error);
  }
});

app.post("/api/push/unsubscribe", requireSupabaseUser, async (req, res, next) => {
  try {
    const endpoint = req.body?.endpoint;
    if (!endpoint) {
      return res.status(400).json({ error: "endpoint is required." });
    }

    await deleteSubscriptionByEndpoint(endpoint);
    return res.json({ status: "unsubscribed" });
  } catch (error) {
    return next(error);
  }
});

app.post("/api/push/send", requirePushAdmin, async (req, res, next) => {
  try {
    const {
      targetUserId,
      segment,
      title,
      body,
      url,
      icon,
      badge,
      data,
    } = req.body || {};

    const subscriptions = await findSubscriptions({ targetUserId, segment });
    const payload = buildPushPayload({ title, body, url, icon, badge, data });
    const results = await sendPushNotifications({ subscriptions, payload });

    return res.json({
      targeted: subscriptions.length,
      sent: results.filter(item => item.status === "sent").length,
      deletedStale: results.filter(item => item.status === "deleted_stale").length,
      failed: results.filter(item => item.status === "failed").length,
      results,
    });
  } catch (error) {
    return next(error);
  }
});

app.use((error, _req, res, _next) => {
  console.error("[push-api]", error);
  res.status(400).json({
    error: error.message || "Push API request failed.",
  });
});

app.listen(port, () => {
  console.log(`PickBook Push API listening on port ${port}`);
});
