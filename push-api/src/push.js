import webPush from "web-push";
import { supabaseAdmin } from "./supabase.js";

const {
  VAPID_PUBLIC_KEY,
  VAPID_PRIVATE_KEY,
  VAPID_SUBJECT,
} = process.env;

if (!VAPID_PUBLIC_KEY || !VAPID_PRIVATE_KEY || !VAPID_SUBJECT) {
  throw new Error("VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, and VAPID_SUBJECT are required.");
}

webPush.setVapidDetails(VAPID_SUBJECT, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY);

export function publicVapidKey() {
  return VAPID_PUBLIC_KEY;
}

export function validateSubscription(subscription) {
  if (!subscription || typeof subscription !== "object") {
    throw new Error("subscription object is required.");
  }

  const { endpoint, keys } = subscription;
  if (!endpoint || typeof endpoint !== "string" || !endpoint.startsWith("https://")) {
    throw new Error("subscription.endpoint must be an HTTPS URL.");
  }

  if (!keys || typeof keys.p256dh !== "string" || typeof keys.auth !== "string") {
    throw new Error("subscription.keys.p256dh and subscription.keys.auth are required.");
  }

  return {
    endpoint,
    p256dh: keys.p256dh,
    auth: keys.auth,
  };
}

export async function upsertPushSubscription({ userId, subscription, userAgent }) {
  const normalized = validateSubscription(subscription);

  const { data, error } = await supabaseAdmin
    .from("push_subscriptions")
    .upsert(
      {
        user_id: userId,
        endpoint: normalized.endpoint,
        p256dh: normalized.p256dh,
        auth: normalized.auth,
        user_agent: userAgent || null,
      },
      { onConflict: "endpoint" },
    )
    .select("id, user_id, endpoint, updated_at")
    .single();

  if (error) {
    throw error;
  }

  return data;
}

export async function deleteSubscriptionByEndpoint(endpoint) {
  if (!endpoint) {
    return;
  }

  await supabaseAdmin
    .from("push_subscriptions")
    .delete()
    .eq("endpoint", endpoint);
}

export async function findSubscriptions({ targetUserId, segment }) {
  let query = supabaseAdmin
    .from("push_subscriptions")
    .select("id, user_id, endpoint, p256dh, auth");

  if (targetUserId) {
    query = query.eq("user_id", targetUserId);
  } else if (segment === "all") {
    query = query.limit(10000);
  } else {
    throw new Error("targetUserId or segment='all' is required.");
  }

  const { data, error } = await query;
  if (error) {
    throw error;
  }

  return data || [];
}

export function buildPushPayload({ title, body, url, icon, badge, data }) {
  if (!title || !body) {
    throw new Error("title and body are required.");
  }

  return JSON.stringify({
    title: String(title).slice(0, 120),
    body: String(body).slice(0, 240),
    icon,
    badge,
    data: {
      ...(data && typeof data === "object" ? data : {}),
      url: url || "/",
    },
  });
}

export async function sendPushNotifications({ subscriptions, payload }) {
  const results = await Promise.allSettled(
    subscriptions.map(async row => {
      const pushSubscription = {
        endpoint: row.endpoint,
        keys: {
          p256dh: row.p256dh,
          auth: row.auth,
        },
      };

      try {
        await webPush.sendNotification(pushSubscription, payload);
        return { endpoint: row.endpoint, status: "sent" };
      } catch (error) {
        if (error.statusCode === 404 || error.statusCode === 410) {
          await deleteSubscriptionByEndpoint(row.endpoint);
          return { endpoint: row.endpoint, status: "deleted_stale", statusCode: error.statusCode };
        }

        return {
          endpoint: row.endpoint,
          status: "failed",
          statusCode: error.statusCode || 500,
          message: error.body || error.message,
        };
      }
    }),
  );

  return results.map(result => (
    result.status === "fulfilled"
      ? result.value
      : { status: "failed", message: result.reason?.message || "Unknown push failure." }
  ));
}
