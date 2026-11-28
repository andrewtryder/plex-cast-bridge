# Plex Cast Bridge voice cold-start experiment

**Date:** 2026-11-27
**Runtime:** Existing Docker installation, running image label `0.6.1`
**Target:** Configured Cast display (virtual name “<configured device>”)

**Phase 2 result:** The independent one-shot fallback successfully started audible playback from a configured library artist after fresh voice `resume` and `start` commands. The exact “play music on <configured device>” phrase did not route reliably in the final trials and did not reach the cloud gateway.

## Results

| Test | Expected | Observed |
|---|---|---|
| Local cold start | Plex search loads and plays on the idle Nest | The explicit `/api/command` request exceeded the probe's original 5-second HTTP timeout, but subsequent bridge status reported `PLAYING` with 11 tracks queued. Cast activity recorded buffering then playing. Position remained unchanged in repeated status reads, so advancing position was not verified. The targeted stop request returned `IDLE`; subsequent status was `UNKNOWN` with no current track or queue. |
| Google Home app On | Cloud fulfillment publishes an OnOff command; bridge receives it | Confirmed. Cloud gateway handled EXECUTE and published; bridge received and completed `resume`. It did not load music. |
| Voice: “Hey Google, turn on <configured device>.” | OnOff/resume reaches bridge | Google replied with an affirmative On response. The corresponding bridge command was `mute`, which completed. The command's mute boolean is not present in sanitized logs. No track or queue appeared. |
| Voice: “Hey Google, resume <configured device>.” | Resume reaches bridge | Google gave a resume confirmation. The cloud gateway published `resume`; bridge received and completed it. No track or queue appeared. |
| Voice: “Hey Google, play music on <configured device>.” | A media request reaches bridge | Google replied with a start confirmation. The bridge received and completed `resume`, not `play`. No track or queue appeared. |
| Voice: “Hey Google, start <configured device>.” | Start request reaches bridge | Google gave an On confirmation. The bridge received and completed `resume`, not `play`. No track or queue appeared. |
| Voice: “Hey Google, play an absent artist on <configured device>.” | Artist/query reaches fulfillment | Google said it could not play media. Cloud gateway logged `action.devices.QUERY` only; no EXECUTE, Pub/Sub publish, or bridge command followed. |
| Voice: “Hey Google, play songs by an absent artist on <configured device>.” | Artist/query reaches fulfillment | Google said it was unable to play music. Cloud gateway again logged QUERY only; no EXECUTE, publish, or bridge command followed. |
| Voice: “Hey Google, play a known artist on <configured device>.” | Known in-library artist reaches fulfillment | Google refused. No matching fulfillment request appeared in gateway logs through 20:58:39 UTC, and no bridge command appeared. |

The user confirmed one tested artist was not in their Plex library. That would prevent a successful Plex search if Google passed the query through, but neither phrase reached Plex. A known in-library artist worked through the local API, but Google still did not route the spoken music request to fulfillment.

## Sanitized evidence

Representative existing log events (UTC; device identifiers and room names redacted):

```text
20:37:48 cloud gateway: Handling action.devices.EXECUTE; publishing command
20:37:50 bridge: Google Home command received/completed action=resume (targeted device)
20:39:40 bridge: Google Home command received/completed action=mute (targeted device)
20:41:36, 20:42:50, 20:45:05 bridge: Google Home command received/completed action=resume
20:51:43 cloud gateway: Handling action.devices.QUERY; no EXECUTE or publish followed
20:56:00 cloud gateway: Handling action.devices.QUERY; no EXECUTE or publish followed
```

The bridge worker intentionally does not log artist/query text. No Pub/Sub messages were manually pulled, acknowledged, or injected. Cloud gateway and Docker logs were read using existing access.

## Conclusions

- The existing infrastructure can deliver Google Home app commands through the cloud gateway, Pub/Sub, and the local bridge.
- Local Plex-to-Cast cold start works for a configured library artist. The bridge's `resume()` only calls Cast `play()` and cannot load media into a device with no session.
- App On and spoken resume-like phrases reached the bridge as resume actions but did not start playback. Google gave verbal success responses despite no Plex track playing.
- Spoken artist requests were rejected before an EXECUTE reached the bridge. Arbitrary artist text therefore did not reach an existing trait or Plex search in these trials. The `selectChannel` and `appSearch` parser paths remain unverified experimentally.
- During the initial Phase 1 run, the experimental fallback was not run because the selected speaker reported `UNKNOWN`, not verified `IDLE`. No current track or queue was reported, but the probe conservatively refused to infer idle from UNKNOWN. The Phase 2 direct receiver check below resolved that uncertainty.

## Smallest justified production change

For the confirmed app-On/resume cold-start failure, add a deliberate cold-start policy for an idle `resume`/On command: load a configured, known Plex selection when no Cast media session exists. Keep normal resume behavior for an existing paused session. This would address the predefined-selection objective only. Supporting arbitrary spoken artist names also requires Google to deliver a supported parameterized media EXECUTE; these trials provide no evidence that a local bridge change alone can make Google route those requests.

No production code, Google Home integration/account linking, container image, or GCP resource was changed. The only repository additions are the disposable diagnostic, its offline tests, and this report.

## Phase 2 direct Cast follow-up

The live bridge's `UNKNOWN` was resolved with a read-only receiver/media query using the PyChromecast package already installed in the running v0.6.1 image:

```text
PyChromecast: 14.1.1
Cast socket connected: true
Receiver status response: received
Media status response: received
Receiver app: CC1AD845 / Default Media Receiver
Receiver standby: true
Media player_state: UNKNOWN
Media session ID: none
Bridge current track: none
Bridge queue length: 0
```

The receiver status request used `cast.socket_client.receiver_controller.update_status()`; media status used `cast.media_controller.update_status()`. Both callbacks returned. Thus the Nest was responsive and in receiver standby; `UNKNOWN` described the absence of a media session, not a disconnected speaker. The probe now requires successful receiver and media status responses, default receiver standby, no media session, and an empty bridge track/queue before classifying this combination as idle. A disconnected Cast, paused session, active session, other app, or incomplete status response cannot arm fallback.

## Phase 2 live fallback proof

The probe used a direct PyChromecast snapshot plus the bridge's current track and queue to establish the precondition. It observed a connected socket, successful receiver and media status responses, Default Media Receiver (`CC1AD845`) in standby, no media session, bridge state `UNKNOWN`, no current Plex track, and an empty queue. It armed only after this positive evidence.

| Spoken command | Fulfillment and bridge result | Fallback and playback result |
|---|---|---|
| “Hey Google, resume <configured device>.” (spoken to a watch) | Cloud gateway EXECUTE published `resume` for the configured speaker; bridge received and completed it. | Fresh Cast status still showed no media session and standby. The probe issued exactly one local `/api/command` request with a configured artist selection. Plex returned an 11-track queue. Cast then reported `PLAYING`/Plex active; direct status position advanced `0.00 → 12.11 → 24.84 → 37.50` seconds. User confirmed audible playback. |
| “Hey Google, start <configured device>.” | Cloud gateway EXECUTE published `resume`; the bridge received and completed the targeted command. Google gave an On confirmation. | The armed one-shot fallback loaded the configured artist selection (11 queued). Cast reported `PLAYING`; position advanced `0.00 → 11.71` seconds, and the user reported playback. |
| “Hey Google, resume <configured device>.” with an existing paused Plex session | Cloud gateway EXECUTE published `resume`; bridge received and completed it. | The probe started with fallback disarmed because Cast positively classified the baseline as `plex_paused`. The same session and queue resumed. Position advanced `148.08 → 158.79` seconds. No fallback request was sent; the user reported playback. |

For the paused-session case, the receiver returned media session ID `1`, player state `PAUSED`, and content type `audio/mpeg`; bridge track and queue were present. After resume, the same track and queue were still present and the direct Cast state was active. This verifies normal resume without playlist replacement.

Representative Phase 2 lifecycle logs (UTC; query text and Pub/Sub message IDs omitted):

```text
21:45:52 Cloud gateway: action.devices.EXECUTE; publishing targeted action=resume
21:45:53 bridge: received/completed targeted action=resume
probe: connected Default Media Receiver; standby=true; media_session_id=null; no bridge track/queue
probe: one-shot POST /api/command; Plex resolved the configured artist; queue_length=11
probe: Cast classification=plex_active; position advanced 0.00s -> 12.11s -> 24.84s -> 37.50s
21:48:10 Cloud gateway: action.devices.EXECUTE; publishing targeted action=resume
21:48:12 bridge: received/completed targeted action=resume
21:50:06 Cloud gateway: action.devices.EXECUTE; publishing targeted action=resume
21:50:06 bridge: received/completed targeted action=resume
21:53:04 Cloud gateway: action.devices.EXECUTE; publishing targeted action=resume
21:53:04 bridge: received/completed targeted action=resume
```

## Exact “play music” phrase and remaining routing limit

The exact phrase initially reached the cloud gateway once as a `resume` but produced no playback before the fallback was enabled. In later hardware runs, an attempted phrase was rejected by the Nest and a watch replied that it could not control media playback. The cloud gateway showed only routine reconciliation requests for those attempts; no fulfillment QUERY/EXECUTE or bridge command appeared. The fallback therefore cannot act when Google does not dispatch a command. The successful `resume` and `start` trials prove the independent cold-start POC through the existing infrastructure, while the exact “play music” phrase remains inconsistent upstream of fulfillment.

The user confirmed audible playback for the first fallback run. The second start run was reported as playing. The paused-session resume was also reported as playing. No other speaker was targeted; the test volume remained within the bridge's configured maximum. The targeted playback was stopped between runs and after the initial playback test.

The updated focused offline suite at the time had 15 passing tests, including UNKNOWN with positive idle evidence, disconnected UNKNOWN, paused/active Plex sessions, other-app media, stale/duplicate/wrong-device/mute commands, exactly-once triggering, and timeout inspection without retry. Syntax checks passed. PyChromecast and cloud gateway/Docker logs were read using existing runtime access; no Pub/Sub messages were manually pulled, acknowledged, or injected.
