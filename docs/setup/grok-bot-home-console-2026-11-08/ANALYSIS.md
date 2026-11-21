# Google Home setup — console walkthrough + usability notes (2026-11-08)

Operator: Cursor agent + Chrome (logged-in). Grok Bot: human-pasted task (Computer Use not authorized for agent drive).

## Environment after cutover

| Item | Value |
|------|--------|
| GCP project | `plex-cast-bridge-1ffe19` (ACTIVE) |
| Cloud Run | `https://plex-google-home-fulfillment-mhjau2rbpa-uc.a.run.app` (`/health` ok, `catalog_storage.storage_ok`) |
| LXC 112 | Pub/Sub enabled; new subscriber key installed; Plex JWT preserved |
| Wizard state | Reset to new project; `link_confirmed=false` |
| Stale GCP | `plex-cast-bridge`, `plex-cast-bridge-home`, `plex-cast-bridge-home-1571a`, `pcb-1ffe19` → `DELETE_REQUESTED` |

## Screenshots

The original screenshots are intentionally not retained; this note records the relevant findings.

## Confirmed defects / friction (console)

1. **Create vs Import trap (P0)**
   Home Developer Console “Create a project” also creates a **new GCP project** (`pcb-1ffe19`). That diverged from the provisioned project `plex-cast-bridge-1ffe19`. Wizard copy must push **Import existing GCP project**, not Create.

2. **Duplicate name with empty list (P1)**
   List showed “You don't have any projects yet”, but name `Plex Cast Bridge` failed with “Project name already exists”. Recovery path unclear.

3. **Orphan Home Console project after GCP delete (P1)**
   After deleting GCP `pcb-1ffe19`, Home Console still listed **PCB 1ffe19**. Project details had no Delete control; GCP logging link returned “service unavailable”.

4. **Import project error (P0)**
   “Import project” returned snackbar **“Request contains an invalid argument.”** with no picker UI — blocked attaching `plex-cast-bridge-1ffe19`.

5. **Actions Console Smart Home deprecated (P0 informational)**
   Creating/onboarding in Actions Console for `plex-cast-bridge-1ffe19` lands on a card: Smart Home must be built in **Google Home Developer Console** (“Go to Developer Console”). Old Actions-only tutorials are stale.

6. **Transient “service unavailable” (P2)**
   Cloud-to-cloud panel / project details briefly showed Google service unavailable banners.

7. **Stale local wizard state (P0, fixed on LXC)**
   `data/google_home_setup.json` still marked all steps complete and linked to deleted Cloud Run URL `…gjkd3pqinq…` until reset. Diagnostics previously showed “home_link connected” falsely.

## Suggestions (wizard UX)

- Step “Connect Google Cloud”: prefer **Import / use existing project ID** with the provisioned ID prefilled after companion provision.
- Explicit warning: “Create project in Home Console creates a *different* Google project — do not do this if the companion already created GCP resources.”
- Never show link success from `link_confirmed` alone; require backend verify (already partially implemented).
- Add recovery when Home Console import fails (invalid argument): retry, open Actions overview, contact support links.
- Tutorials must show Home Developer Console, not legacy Actions Smart Home create flow.

## Grok Bot status

- Task prompt written to `GROK_BOT_TASK.md` and pasted by operator into Grok Bot.
- Agent **cannot** drive Grok Bot until Cursor Computer Use has **Accessibility** + **Screen Recording**.
- Do not claim Grok completed analysis until its report is captured here.

## Remaining human steps

1. Enable Computer Use permissions if you want the agent to drive Grok Bot, **or** paste Grok’s report back into chat.
2. Resolve Home Console attachment of `plex-cast-bridge-1ffe19` (import) and remove zombie **PCB 1ffe19**.
3. Create Cloud-to-cloud integration with OAuth/fulfillment URLs for the new Cloud Run service (secrets from local `gcp-cloud-run/.env` only).
4. Phone: link `[test]` integration in Google Home app; assign rooms.
5. Verify catalog sync, Request Sync, Pub/Sub, voice command.

## Update — Import picker

- Get-started has an **Existing project** combobox (`aria-label: Select project`).
- Import without a selection → `Request contains an invalid argument.`
- Import after failed selection → `Can't create project. Please try again.`
- “Manage projects” menuitem appeared once but did not open a usable picker in automation (possible Google account chooser / popup blocked).
- Actions onboarding for `plex-cast-bridge-1ffe19` explicitly redirects Smart Home to Home Developer Console.

**Human assist needed:** In Chrome on https://console.home.google.com/projects/get-started , open **Existing project**, pick **plex-cast-bridge-1ffe19** / **Plex Cast Bridge**, then **Import project**. Delete zombie **PCB 1ffe19** if still listed.

## Grok Bot / github agent report (captured via Computer Use)

Source: Grok Bot app → **github** chat → document **Plex Cast Bridge Google Home setup — UX review (PARTIAL)** (~8:23pm Nov 8, 2026).

### Coverage
- Home Console empty / get-started / create form: **confirmed** (no create/import submitted in that review)
- Create/import success, C2C handoff, phone linking: **unverified / human**
- Local wizard `http://192.168.88.130:8000`: **blocked HTTP 400** in Grok’s browser

### Priority summary (from Grok table)

| Pri | Class | Finding |
|-----|-------|---------|
| P1 | Suggestion | Empty Home Console dashboard steers first-time users toward Create |
| P1 | Suggestion | Create form makes Create primary and Import secondary |
| P1 | Historical | Create New Home project provisioned different GCP project (`pcb-1ffe19`) |
| P1 | Historical | Local wizard showed stale completed / `link_confirmed` for deleted Cloud Run |
| P2 | Confirmed | Existing project picker lacks display-name / ID pairing |
| P2 | Historical | Name `Plex Cast Bridge` rejected as already exists while list empty |
| P3 | Historical | Cloud-to-cloud section routed to Actions on Google |
| Blocked | Blocked | Local bridge wizard not inspected — HTTP 400 |
