# Grok Bot task — Google Home Developer Console + Plex Cast Bridge wizard UX review

Act as a first-time user and usability reviewer. Do **not** change billing, IAM, GCP resources, or authorize Google Home account linking without human approval.

## Context
- New GCP project: `plex-cast-bridge-1ffe19` (display name Plex Cast Bridge)
- New Cloud Run: `https://plex-google-home-fulfillment-mhjau2rbpa-uc.a.run.app`
- Local bridge: http://192.168.88.130:8000 (LXC 112) — Plex JWT preserved
- Home Developer Console currently empty; we are creating a fresh Cloud-to-cloud integration

## Your job
1. Open https://console.home.google.com/projects and observe the empty state / create-project flow.
2. Walk the wizard on the local bridge dashboard Google Home setup (via browser on the LAN).
3. At each step note: is the instruction clear? Is the primary action obvious? Is success criteria clear?
4. Screenshot confusing UI (redact secrets).
5. Produce a prioritized usability report: confirmed defects vs suggestions.

## Constraints
- Do not enter client secrets into recordings/chat.
- Do not bypass Google login/account-linking security.
- Stop and hand off for phone linking in Google Home app.
