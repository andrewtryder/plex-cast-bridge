// Dashboard session and pairing authentication

import { $, announce, errorDetail, readJson } from "./util.js";

const LEGACY_STORAGE_KEY = "plex_cast_api_key";
let legacyApiKey = "";

export function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (legacyApiKey) headers["X-API-Key"] = legacyApiKey;
  return headers;
}

export const migrationReady = (async () => {
  const stored = localStorage.getItem(LEGACY_STORAGE_KEY);
  if (!stored) return;
  try {
    const res = await fetch("/api/session/migrate", {
      method: "POST",
      headers: { "X-API-Key": stored },
    });
    if (res.ok) {
      localStorage.removeItem(LEGACY_STORAGE_KEY);
      return;
    }
  } catch (_) {}
  legacyApiKey = stored;
})();

export async function refreshDashboardRequests() {
  const container = $("dashboardPendingRequests");
  if (!container) return;
  try {
    const res = await fetch("/api/session/pending", { headers: authHeaders() });
    if (!res.ok) {
      container.replaceChildren();
      return;
    }
    const data = await readJson(res);
    const requests = data?.requests || [];
    container.replaceChildren();
    if (!requests.length) {
      const empty = document.createElement("p");
      empty.className = "conn-meta";
      empty.textContent = "No browsers waiting for approval.";
      container.appendChild(empty);
      return;
    }
    requests.forEach((req) => {
      const row = document.createElement("div");
      row.className = "conn-row";
      const info = document.createElement("div");
      const codeLabel = document.createElement("strong");
      codeLabel.textContent = req.code;
      const meta = document.createElement("span");
      meta.className = "conn-meta";
      meta.style.marginLeft = "0.5rem";
      meta.textContent = `requested ${new Date(req.created * 1000).toLocaleTimeString()}`;
      info.append(codeLabel, meta);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "plain-button";
      btn.textContent = "Approve";
      btn.addEventListener("click", async () => {
        try {
          const approveRes = await fetch("/api/session/approve", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders() },
            body: JSON.stringify({ code: req.code }),
          });
          if (!approveRes.ok) throw new Error("Approval failed");
          announce(`Approved code ${req.code}`);
          await refreshDashboardRequests();
        } catch (err) {
          announce(err.message || "Approval failed");
        }
      });

      row.append(info, btn);
      container.appendChild(row);
    });
  } catch (_) {
    container.replaceChildren();
  }
}

export function initSessionUi(onAuthSuccess) {
  if (location.protocol === "http:") {
    const w1 = $("dashboardHttpWarning");
    const w2 = $("dashboardSessionWarning");
    if (w1) w1.hidden = false;
    if (w2) w2.hidden = false;
  }

  const reqBtn = $("dashboardRequestAccess");
  if (reqBtn) {
    reqBtn.addEventListener("click", async () => {
      reqBtn.disabled = true;
      const statusEl = $("dashboardAccessStatus");
      if (statusEl) statusEl.textContent = "Requesting access code…";
      try {
        const res = await fetch("/api/session/request", { method: "POST" });
        const data = await readJson(res);
        if (!res.ok) throw new Error(errorDetail(data, "Request failed"));
        if (statusEl) {
          statusEl.textContent = `Pairing code: ${data.code}. Approve this code in an authorized browser or CLI.`;
        }
        let attempts = 0;
        const interval = setInterval(async () => {
          attempts++;
          if (attempts > 60) {
            clearInterval(interval);
            reqBtn.disabled = false;
            if (statusEl) statusEl.textContent = "Request timed out. Try again.";
            return;
          }
          try {
            const claimRes = await fetch("/api/session/claim", { method: "POST" });
            const claimData = await readJson(claimRes);
            if (claimData?.status === "authenticated") {
              clearInterval(interval);
              $("dashboardAccess").hidden = true;
              announce("Dashboard access granted");
              if (onAuthSuccess) onAuthSuccess();
            }
          } catch (_) {}
        }, 3000);
      } catch (err) {
        reqBtn.disabled = false;
        if (statusEl) statusEl.textContent = err.message || "Request failed";
      }
    });
  }

  $("dashboardRefreshRequests")?.addEventListener("click", refreshDashboardRequests);

  $("dashboardRevokeOthers")?.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/session/revoke-others", {
        method: "POST",
        headers: authHeaders(),
      });
      if (res.ok) announce("Other browser sessions revoked");
      else announce("Failed to revoke other sessions");
    } catch (_) {
      announce("Failed to revoke other sessions");
    }
  });

  $("dashboardLogout")?.addEventListener("click", async () => {
    try {
      await fetch("/api/session/logout", {
        method: "POST",
        headers: authHeaders(),
      });
      localStorage.removeItem(LEGACY_STORAGE_KEY);
      location.reload();
    } catch (_) {
      location.reload();
    }
  });
}
