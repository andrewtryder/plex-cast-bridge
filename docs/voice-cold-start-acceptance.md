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
| Individual reports IDLE while its static Cast Group is playing | Cold start is skipped; group playback remains audible and unchanged. |
| Individual reports UNKNOWN while its Cast Group is playing or paused | Cold start is skipped; no receiver is launched and group playback remains unchanged. |
| Destination belongs to overlapping groups, with any group playing or paused | Cold start is skipped until every applicable group has fresh, connected idle status. |
| Group playback begins while Plex selection resolves | Fresh revalidation prevents the cold-start load. |
| Group membership is stale, unavailable, disconnected, or being rediscovered | Classification is indeterminate and no load occurs. |
| Fresh membership and media state confirm no conflicting group session | Ordinary idle authorization may proceed. |
| Empty or changing mDNS inventory | No-group status stays indeterminate until two stable, nonempty discovery passes. |
| External media playing or paused in Default Media Receiver, with a stale nonempty bridge queue | Treat media as external; do not resume, replace, stop, or otherwise interrupt it. |
| Receiver disconnected, status times out, or state is uncertain | No fallback media is loaded; logs/activity identify a skipped state. |
| Plex disconnected or switched during selection resolution | Discard the selection from the old connection; no media is loaded. |
| Dashboard name-based lookup and Google Home device-ID lookup target the same Cast UUID | Both operations use the same queue, lock, generation, and ownership state. |
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

Cast Group safety also needs physical verification before hardware acceptance:
with a second Cast member present, exercise static and dynamic groups, individual
IDLE and UNKNOWN reporting, active and paused group sessions, overlapping group
membership, group creation/removal during selection, and group-controller
disconnect/reconnection. Confirm no group member is interrupted. Include an
audio-only Nest target and a group-targeted voice command. The current connected
Nest diagnostic with an inactive media namespace remains `indeterminate`; there
is not enough independent status evidence to authorize it as idle. Do not relax
that rule unless a read-only receiver diagnostic and controlled hardware test
establish a reliable no-media-application signal.

The bridge serializes its own queue commands through the speaker queue lock and
generation. Independent Cast senders cannot share that lock; a sender can still
change receiver state in the small interval between final status validation and
the Cast load. Include an external sender race in controlled testing where
available, and treat it as a residual limitation if it cannot be made
deterministic. Pub/Sub dedupe is local, bounded to 2,048 IDs and 31 days; after
an ID expires or is evicted under sustained volume, a very late redelivery is
not guaranteed to be suppressed.

## Pub/Sub and persistent-state isolation

Never run the candidate and the production bridge at the same time with the
same Pub/Sub subscription. Either subscriber could acknowledge and consume the
other subscriber's Google Home command.

For local-only candidate checks, keep the candidate Pub/Sub listener disabled.
Use a separate candidate data directory and a separately prepared copy of the
required Plex authorization, speaker catalog, and local cold-start policy. Do
not mount the production data directory read/write. Do not configure
`CLOUD_GATEWAY_URL` or `CATALOG_WRITE_KEY` in the candidate environment, and
remove those fields and pending push state from the candidate copy of
`google_home_sync.json`. Do not use the candidate dashboard to link Google
Home, pair accounts, or change the cloud speaker catalog. This keeps catalog
uploads, reconciliation, sync bookkeeping, and persistent settings away from
production while exercising local playback against the test speaker.

End-to-end voice acceptance requires the existing cloud gateway to publish to
the subscription the candidate consumes. If no separate pre-existing test
subscription is available, conduct it only as an explicitly approved
maintenance-window replacement:

1. Record the live image digest, configuration, current health, and a verified
   backup. Prepare the candidate's separate data directory and verify it does
   not resolve to or overlap the production mount. Preconfigure its local
   speaker policy while the Pub/Sub listener is disabled and cloud sync fields
   are absent.
2. Stop the live bridge and verify that it is no longer running and has closed
   its subscription connection. Do not start the candidate before this check.
3. Start the candidate as the sole subscriber to the existing subscription,
   with the prepared separate data directory, the existing subscriber
   credentials, Pub/Sub project/subscription identifiers, and cloud sync
   disabled. Confirm candidate health before issuing any voice command.
4. Run only the approved acceptance cases. Do not edit cloud catalog/link
   settings or run first-time setup. Record command, bridge activity, Cast
   ownership/session, and audible result.
5. Stop the candidate before restarting production. Preserve candidate logs
   and its test data separately; do not copy its catalog or sync state over the
   production data directory. Restore the recorded production image and
   original data mount, then verify health and subscription reconnection.
6. If the candidate fails, stop it first and perform the same image-only
   rollback with the untouched production data directory. Verify Cast playback
   and bridge health before closing the maintenance window.

This replacement procedure interrupts normal bridge service and changes which
process consumes the production subscription. It requires explicit operator
approval before any stop, start, or data-mount operation. Candidate local
dedupe state is separate from production; after rollback, a redelivered command
may not have the candidate's message ID in production dedupe storage. The
classifier and queue ownership checks must still prevent action on active or
uncertain playback.

## Rollback and evidence

Disable the per-speaker toggle after acceptance. If any safety case replaces
external media or an uncertain receiver, stop the test and roll back the
candidate using the existing image-only rollback procedure. Preserve the
sanitized logs, activity outcomes, exact device ID, candidate digest, and
before/after Cast state with the acceptance record. Do not publish or promote a
release until the separate release gates and explicit approval are complete.
