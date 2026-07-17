const DEFAULT_VAPID_PUBLIC_KEY_ENDPOINT = "/api/push/vapid-public-key";
const DEFAULT_SUBSCRIBE_ENDPOINT = "/api/push/subscribe";

export function isWebPushSupported() {
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = `${base64String}${padding}`.replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const output = new Uint8Array(rawData.length);

  for (let index = 0; index < rawData.length; index += 1) {
    output[index] = rawData.charCodeAt(index);
  }

  return output;
}

async function getVapidPublicKey(endpoint) {
  const response = await fetch(endpoint, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new Error(`Could not load VAPID public key (${response.status}).`);
  }

  const payload = await response.json();
  if (!payload.publicKey) {
    throw new Error("VAPID public key response is missing publicKey.");
  }

  return payload.publicKey;
}

export async function requestNotificationPermission() {
  if (!isWebPushSupported()) {
    throw new Error("This browser does not support web push notifications.");
  }

  if (Notification.permission === "granted") {
    return "granted";
  }

  if (Notification.permission === "denied") {
    throw new Error("Notifications are blocked for PickBook in this browser.");
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notification permission was not granted.");
  }

  return permission;
}

export async function subscribePickBookUserToPush({
  userId,
  accessToken,
  serviceWorkerPath = "/service-worker.js",
  vapidPublicKeyEndpoint = DEFAULT_VAPID_PUBLIC_KEY_ENDPOINT,
  subscribeEndpoint = DEFAULT_SUBSCRIBE_ENDPOINT,
} = {}) {
  if (!userId) {
    throw new Error("A signed-in PickBook userId is required before subscribing.");
  }

  await requestNotificationPermission();

  const registration = await navigator.serviceWorker.register(serviceWorkerPath);
  await navigator.serviceWorker.ready;

  const vapidPublicKey = await getVapidPublicKey(vapidPublicKeyEndpoint);
  const applicationServerKey = urlBase64ToUint8Array(vapidPublicKey);

  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey,
    });
  }

  const response = await fetch(subscribeEndpoint, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: JSON.stringify({
      userId,
      subscription: subscription.toJSON(),
    }),
  });

  if (!response.ok) {
    const errorPayload = await response.json().catch(() => ({}));
    throw new Error(errorPayload.error || `Could not save push subscription (${response.status}).`);
  }

  return {
    subscription,
    server: await response.json(),
  };
}

export async function getExistingPushSubscription(serviceWorkerPath = "/service-worker.js") {
  if (!isWebPushSupported()) {
    return null;
  }

  const registration = await navigator.serviceWorker.register(serviceWorkerPath);
  await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}
