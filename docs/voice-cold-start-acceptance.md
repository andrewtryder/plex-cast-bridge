# Voice Cold Start Candidate Acceptance

This plan is for the next approved candidate-image stage. It does not authorize
replacing the live v0.6.1 container. Use the repository's [verified release
process](release-process.md) to produce an immutable candidate digest after the
feature is reviewed and merged; record that exact digest before hardware work.

## Before the controlled test

1. Confirm the live installation is still on v0.6.1 and save its existing
   backup using the normal upgrade procedure.
2. Record the candidate version, commit, immutable image digest, bridge health,
   and target Cast device's stable Google Home ID.
3. Upgrade only the explicitly selected test installation using the existing
   backup and rollback procedure. Confirm the bridge starts and the existing
   Google Home link, Plex connection, and speaker catalog remain intact.
4. In **Speakers**, confirm Voice Cold Start is off by default for an existing
   speaker. Enable it only on the test speaker and configure either library
   shuffle or an artist known to exist in Plex.
5. Verify the chosen Google Home name and stable ID. Use the verified phrase
   **“Hey Google, resume <configured speaker name>.”** Do not use artist-search
   wording or “play music on …” as an acceptance criterion; Google routing for
   those phrases was inconsistent in the independent experiment.

## Acceptance cases

Run each case on the isolated test speaker and record Google's exact response,
the bridge activity entry, Cast media session/track/queue state, and audible
result where expected.

| Starting state | Expected result |
| --- | --- |
| Positively confirmed idle receiver; policy enabled; valid selection | One configured selection is initiated and becomes audible. |
| Same speaker, second duplicate resume during startup | No second queue replacement. |
| Paused Plex track with queued items | Same track and queue resume; no replacement. |
| Active Plex playback | Existing playback continues unchanged. |
| Active media from another Cast application | Media remains untouched. |
| External media playing or paused in Default Media Receiver, with a stale nonempty bridge queue | Treat media as external; do not resume, replace, stop, or otherwise interrupt it. |
| Receiver disconnected, status times out, or state is uncertain | No fallback media is loaded; logs/activity identify a skipped state. |
| Plex disconnected or switched during selection resolution | Discard the selection from the old connection; no media is loaded. |
| Dashboard queue command or stop races selection resolution | Queue generation change wins; the cold-start request is skipped and the user's local command remains authoritative. |
| Pub/Sub redelivery after restart or after the short duplicate window | The same message ID does not initiate a second cold start during the bounded 31-day dedupe retention. |
| Policy disabled on confirmed idle speaker | Existing transport behavior remains; no cold-start selection is loaded. |
| Missing artist or empty library | No unrelated music is substituted; outcome is a safe selection failure. |
| Pause, stop, mute, or volume command while idle | No cold start is triggered. |

Afterward, restart the test container, refresh speaker discovery, and change
the friendly Google Home name. Confirm the per-speaker toggle and selection
remain attached to the stable ID. Export a backup and verify the catalog entry
is present. Confirm the Cloud Run catalog mirror contains no local cold-start
preferences.

The bridge serializes its own queue commands through the speaker queue lock and
generation. Independent Cast senders cannot share that lock; a sender can still
change receiver state in the small interval between final status validation and
the Cast load. Include an external sender race in controlled testing where
available, and treat it as a residual limitation if it cannot be made
deterministic. Pub/Sub dedupe is local, bounded to 2,048 IDs and 31 days; after
an ID expires or is evicted under sustained volume, a very late redelivery is
not guaranteed to be suppressed.

## Rollback and evidence

Disable the per-speaker toggle after acceptance. If any safety case replaces
external media or an uncertain receiver, stop the test and roll back the
candidate using the existing image-only rollback procedure. Preserve the
sanitized logs, activity outcomes, exact device ID, candidate digest, and
before/after Cast state with the acceptance record. Do not publish or promote a
release until the separate release gates and explicit approval are complete.
