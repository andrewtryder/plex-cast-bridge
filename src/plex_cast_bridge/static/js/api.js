// Authenticated API client for Plex Cast Bridge

import { authHeaders, migrationReady } from "./session.js";
import { $, errorDetail, readJson, setConnection } from "./util.js";

let authFailed = false;

export function isAuthFailed() {
  return authFailed;
}

export async function api(path, options = {}) {
  await migrationReady;
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  if (options.body && typeof options.body === "string" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, {
    ...options,
    headers,
  });

  if (res.status === 401) {
    authFailed = true;
    setConnection(false, "Auth required");
    const acc = $("dashboardAccess");
    if (acc) acc.hidden = false;
    throw new Error("Dashboard access requires approval.");
  }

  const data = await readJson(res);
  if (!res.ok) {
    throw new Error(errorDetail(data, `HTTP ${res.status}`));
  }
  authFailed = false;
  setConnection(true, "Local bridge connected");
  return data;
}
