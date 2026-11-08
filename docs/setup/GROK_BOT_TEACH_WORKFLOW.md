# Optional Grok Bot teach-a-task workflow

Grok Bot is **not required** for the Google Home wizard. Use this only if you want a reusable skill from a one-time demonstration.

## Supported capability (as of docs)

Grok Bot’s **Teach a task** records visible computer interaction (up to ~10 minutes, no microphone audio) and drafts a skill. Review and test before scheduling. Credentials must use secure handoff — never type secrets into a recording.

References:

- [xAI Grok Bot skills & routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- [Grok Bot 101](https://x.ai/bot/guides/grok-bot-101)

There is **no unofficial API integration** in this repository. Do not automate Grok Bot via undocumented endpoints.

## Recommended procedure

1. Complete a successful Google Home setup once using the in-app wizard + Developer Console.
2. Open a one-to-one Grok Bot conversation and its computer view.
3. Choose **Teach a task**; describe the outcome (for example: “Configure Plex Cast Bridge Cloud-to-cloud URLs in Google Home Developer Console”).
4. Demonstrate only non-secret clicks: open console, paste fulfillment URL fields, save. Use secure handoff for client secrets.
5. Stop recording; review the draft skill; add failure handling and “require human approval for billing/IAM”.
6. Test on a disposable project.
7. Optionally translate approved steps into `assets/tutorials/manifest.json` entries (links + captions), not as a hard dependency of the wizard.

## Safety

- Never record passwords, OAuth tokens, client secrets, API keys, or service-account JSON.
- Human approval remains required for billing and consequential cloud changes.
