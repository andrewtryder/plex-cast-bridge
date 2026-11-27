# Plex Cast Bridge voice cold-start experiment

**Date:** 2026-11-27
**Runtime:** Docker host `192.168.88.130`; running image label `0.6.1`
**Target:** Master Bedroom display (`plex-jukebox:Master Bedroom`), virtual name “Master Bedroom Plex”

**Phase 2 result:** The independent one-shot fallback successfully started audible DJ Shadow playback after fresh voice `resume` and `start` commands. The exact “play music on Master Bedroom Plex” phrase did not route reliably in the final trials and did not reach Cloud Run.

## Results

| Test | Expected | Observed |
|---|---|---|
| Local cold start | Plex search loads and plays on the idle Nest | The explicit `/api/command` request for DJ Shadow exceeded the probe's original 5-second HTTP timeout, but subsequent bridge status reported `PLAYING`, “Mutual Slump” by DJ Shadow, album *Endtroducing.....*, with 11 tracks queued. Cast activity recorded buffering then playing. Position remained at 536 ms in repeated status reads, so advancing position was not verified. The targeted stop request returned `IDLE`; subsequent status was `UNKNOWN` with no current track or queue. |
| Google Home app On | Cloud fulfillment publishes an OnOff command; bridge receives it | Confirmed. Cloud Run handled EXECUTE and published; bridge received and completed `resume`. It did not load music. |
| Voice: “Hey Google, turn on Master Bedroom Plex.” | OnOff/resume reaches bridge | Google replied “OK. I've turned on the master bedroom plex.” The corresponding bridge command was `mute`, which completed. The command's mute boolean is not present in sanitized logs. No track or queue appeared. |
| Voice: “Hey Google, resume Master Bedroom Plex.” | Resume reaches bridge | Google replied “Resumed master bedroom Plex.” Cloud Run published `resume`; bridge received and completed it. No track or queue appeared. |
| Voice: “Hey Google, play music on Master Bedroom Plex.” | A media request reaches bridge | Google replied “Started Master Bedroom Plex.” The bridge received and completed `resume`, not `play`. No track or queue appeared. |
| Voice: “Hey Google, start Master Bedroom Plex.” | Start request reaches bridge | Google replied “Master Bedroom Plex is now on.” The bridge received and completed `resume`, not `play`. No track or queue appeared. |
| Voice: “Hey Google, play Dire Straits on Master Bedroom Plex.” | Artist/query reaches fulfillment | Google said it could not play media. Cloud Run logged `action.devices.QUERY` only; no EXECUTE, Pub/Sub publish, or bridge command followed. |
| Voice: “Hey Google, play songs by Dire Straits on Master Bedroom Plex.” | Artist/query reaches fulfillment | Google said it was unable to play music. Cloud Run again logged QUERY only; no EXECUTE, publish, or bridge command followed. |
| Voice: “Hey Google, play DJ Shadow on Master Bedroom Plex.” | Known in-library artist reaches fulfillment | Google refused. No matching fulfillment request appeared in Cloud Run logs through 20:58:39 UTC, and no bridge command appeared. |

The user confirmed Dire Straits is not in their Plex library. That would prevent a successful Plex search if Google passed the query through, but neither Dire Straits phrase reached Plex. DJ Shadow is present in the library and worked through the local API, but Google still did not route the spoken music request to fulfillment.

## Sanitized evidence

Representative existing log events (UTC):

```text
20:37:48 Cloud Run: Handling action.devices.EXECUTE; publishing command
20:37:50 bridge: Google Home command received action=resume (Master Bedroom)
20:37:50 bridge: Google Home command completed action=resume (Master Bedroom)
20:39:40 bridge: Google Home command received/completed action=mute (Master Bedroom)
20:41:36, 20:42:50, 20:45:05 bridge: Google Home command received/completed action=resume
20:51:43 Cloud Run: Handling action.devices.QUERY; no EXECUTE or publish followed
20:56:00 Cloud Run: Handling action.devices.QUERY; no EXECUTE or publish followed
```

The bridge worker intentionally does not log artist/query text. No Pub/Sub messages were manually pulled, acknowledged, or injected. Cloud Run and remote Docker logs were read using existing access.

## Conclusions

- The existing infrastructure can deliver Google Home app commands through Cloud Run, Pub/Sub, and the local bridge.
- Local Plex-to-Cast cold start works for DJ Shadow. The bridge's `resume()` only calls Cast `play()` and cannot load media into a device with no session.
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
| “Hey Google, resume Master Bedroom Plex.” (spoken to Pixel Watch) | Cloud Run EXECUTE published `resume` for `plex-jukebox:Master Bedroom`; bridge received and completed it. | Fresh Cast status still showed no media session and standby. The probe issued exactly one local `/api/command` request with `Play songs by DJ Shadow`. Plex resolved DJ Shadow and returned an 11-track queue. Cast then reported `PLAYING`/Plex active; direct status position advanced `0.00 → 12.11 → 24.84 → 37.50` seconds. User confirmed audible playback. |
| “Hey Google, start Master Bedroom Plex.” | Cloud Run EXECUTE published `resume`; the bridge received and completed the exact targeted command. Google replied “Turning on the master bedroom plex.” | The armed one-shot fallback loaded DJ Shadow (11 queued). Cast reported `PLAYING`; position advanced `0.00 → 11.71` seconds, and the user reported DJ Shadow playing. |
| “Hey Google, resume Master Bedroom Plex.” with an existing paused DJ Shadow session | Cloud Run EXECUTE published `resume`; bridge received and completed it. | The probe started with fallback disarmed because Cast positively classified the baseline as `plex_paused`. The track remained “Stem / Transmission 2” (rating key 3325), queue remained 10, and Cast resumed the same session. Position advanced `148.08 → 158.79` seconds. No fallback request was sent; the user reported music playing again. |

For the paused-session case, the receiver returned media session ID `1`, player state `PAUSED`, and content type `audio/mpeg`; bridge track and queue were present. After resume, the same track and queue were still present and the direct Cast state was active. This verifies normal resume without playlist replacement.

Representative Phase 2 lifecycle logs (UTC; query text and Pub/Sub message IDs omitted):

```text
21:45:52 Cloud Run: action.devices.EXECUTE; publishing device plex-jukebox:Master Bedroom action=resume
21:45:53 bridge: received/completed action=resume device_id=plex-jukebox:Master Bedroom
probe: connected Default Media Receiver; standby=true; media_session_id=null; no bridge track/queue
probe: one-shot POST /api/command; Plex resolved target_artist=DJ Shadow; queue_length=11
probe: Cast classification=plex_active; position advanced 0.00s -> 12.11s -> 24.84s -> 37.50s
21:48:10 Cloud Run: action.devices.EXECUTE; publishing device plex-jukebox:Master Bedroom action=resume
21:48:12 bridge: received/completed action=resume device_id=plex-jukebox:Master Bedroom
21:50:06 Cloud Run: action.devices.EXECUTE; publishing device plex-jukebox:Master Bedroom action=resume
21:50:06 bridge: received/completed action=resume device_id=plex-jukebox:Master Bedroom
21:53:04 Cloud Run: action.devices.EXECUTE; publishing device plex-jukebox:Master Bedroom action=resume
21:53:04 bridge: received/completed action=resume device_id=plex-jukebox:Master Bedroom
```

## Exact “play music” phrase and remaining routing limit

The exact phrase initially reached Cloud Run once as a `resume` (Google said “Started Master Bedroom Plex”) but produced no playback before the fallback was enabled. In the later hardware runs, attempts from the Nest were rejected with “Sorry, I don’t recognize that device”; the Pixel Watch replied “I can’t control media playback.” Cloud Run showed only routine reconciliation requests for those attempts; no fulfillment QUERY/EXECUTE or bridge command appeared. The fallback therefore cannot act when Google does not dispatch a command. The successful `resume` and `start` trials prove the independent cold-start POC through the existing infrastructure, while the exact “play music” phrase remains inconsistent upstream of fulfillment.

The user confirmed audible playback for the first fallback run. The second start run was reported as playing DJ Shadow. The paused-session resume was also reported as playing. No other speaker was targeted; the test volume remained within the bridge's configured maximum. The targeted playback was stopped between runs and after the initial playback test.

The updated focused offline suite has 15 passing tests, including UNKNOWN with positive idle evidence, disconnected UNKNOWN, paused/active Plex sessions, other-app media, stale/duplicate/wrong-device/mute commands, exactly-once triggering, and timeout inspection without retry. Syntax checks passed. PyChromecast and Cloud Run/Docker logs were read using existing runtime access; no Pub/Sub messages were manually pulled, acknowledged, or injected.
