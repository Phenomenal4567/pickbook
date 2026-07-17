import { supabaseAdmin } from "./supabase.js";

export async function requireSupabaseUser(req, res, next) {
  const authHeader = req.get("authorization") || "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.slice("Bearer ".length) : "";

  if (!token) {
    return res.status(401).json({ error: "Missing bearer token." });
  }

  const { data, error } = await supabaseAdmin.auth.getUser(token);
  if (error || !data.user) {
    return res.status(401).json({ error: "Invalid or expired bearer token." });
  }

  req.user = data.user;
  return next();
}

export function requirePushAdmin(req, res, next) {
  const expected = process.env.PUSH_ADMIN_TOKEN;
  const supplied = req.get("x-push-admin-token");

  if (!expected || supplied !== expected) {
    return res.status(403).json({ error: "Invalid push admin token." });
  }

  return next();
}
