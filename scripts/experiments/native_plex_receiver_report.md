# Native Plex Cast receiver experiment

**Status: incomplete; hardware trials tabled at the user's request.** No native
Plex playback was started. This report separates source/code findings,
existing-engine evidence, the latest read-only snapshot, and hardware cases
that remain deferred until a safe test window.

## Executive result

PyChromecast 14.1.1 provides a standalone Plex receiver controller, and PlexAPI
can prepare server-managed queues for it. This is a credible optional-backend
prototype, but there is not yet hardware evidence that it improves playback,
queue transitions, Plex history, metadata, group operation, or recovery on the
configured Nest Hub. Keep the current Default Media Receiver backend as the
only playback engine for now. Revisit optional-backend adoption after the
hardware matrix below is completed and compared against the existing engine.

The current Voice Cold Start implementation checks Cast Group membership and
group media state before authorizing an idle-member cold start. An earlier
code-level gap was fixed on this branch by consulting group state, including
when the individual receiver reports `UNKNOWN`. Physical validation of static,
dynamic, overlapping, and changing groups remains deferred; neither a
hardware-confirmed false-idle defect nor hardware acceptance is claimed here.

The Jellyfin comparison adds useful queue lifecycle, media reporting, and
device-profile techniques, but it does not change the current recommendation:
complete native Plex hardware validation before considering a native backend,
and do not start a custom receiver now. A custom receiver does not provide
Google Assistant music-provider registration or arbitrary spoken artist
routing.

## Architecture comparison

| Area | Existing bridge path | Native Plex receiver path | Evidence and limit |
| --- | --- | --- | --- |
| Cast app | Default Media Receiver `CC1AD845` | Plex app `9AC194DC` | IDs confirmed in the bridge and PyChromecast 14.1.1 source. |
| Queue owner | `SpeakerQueue` and its bridge-managed queue | Plex server PlayQueue | Native controller asks PlexAPI/PMS to create or use a PlayQueue. Native queue behavior on this Nest is untested. |
| Track load | Bridge sends a Plex part URL and Cast media metadata | Plex controller sends a Plex receiver command with the PlayQueue and Plex server details | Source inspection only; do not print either authenticated payload. |
| Direct/transcode | Current bridge streams a Plex part URL through Default Media Receiver | Native payload requests direct play/stream by default and permits Plex audio transcoding | Actual Plex session stream decision must be observed on the server during playback; untested. |
| Metadata/art | Bridge supplies title, artist, album, and available thumb URL | Plex receiver can source metadata/art from the Plex queue/item | Native metadata and artwork on the target are untested. |
| Controls | Cast media controller plus bridge queue/history/generation state | `PlexController` play, pause, stop, next, previous | Native track selection and continuity are untested. A successful method call alone is not playback evidence. |
| Google Home | Existing cloud gateway publishes supported commands to the bridge | Native receiver does not change Google fulfillment or device traits by itself | Native playback cannot make Google dispatch a request that never reaches fulfillment. |

## Three architecture options

| Concern | 1. Default Media Receiver + bridge queue | 2. Native Plex receiver | 3. Standalone custom Web Receiver |
| --- | --- | --- | --- |
| Cast application | Google Default Media Receiver `CC1AD845` | Plex receiver `9AC194DC` | New app ID registered to our custom receiver URL |
| Primary queue owner | Bridge `SpeakerQueue`; it currently preloads one upcoming track into the Cast queue | Plex server PlayQueue, controlled by PyChromecast `PlexController` | Whatever the receiver/backend implements; Jellyfin keeps an active playlist and cursor in receiver JavaScript memory |
| Bridge/sender disconnect | Current track and its already-enqueued next item may continue if the receiver app stays alive; further progression depends on the bridge replenishing the one-item lookahead. Not hardware-tested. | A Plex PlayQueue is server-managed, but continuation after sender/bridge disconnect on this target is not hardware-tested. | Jellyfin's receiver-side playlist can advance while the receiver stays loaded. Google documents receiver lifecycle termination conditions that include last-sender disconnect, so queue continuation cannot be assumed. Not hardware-tested. |
| Bridge process restart | Python queue, history, and generation state are process-owned; the Cast app may still hold current/one preloaded item, but restart continuity is unknown. | Server PlayQueue may outlive the control process; connection/session reattachment and actual continuation are unknown. | Receiver JavaScript playlist is not persisted by the reviewed `PlaybackManager`; receiver reload/restart loses that in-memory list. |
| Plex playback reporting | Direct Plex part URL is loaded through Cast; actual session/history behavior remains to be tested. | Plex app and server-managed queue can provide native Plex session semantics; actual session/history behavior remains to be tested. | Must implement Plex session, progress, stop, and listening-history calls explicitly; Jellyfin's reporting code is only an architectural pattern. |
| Device capabilities | Default receiver/selected Cast device handles playback; bridge has no receiver-side `canDisplayType()` probe. | Plex receiver negotiates its own formats; probe must inspect Plex session stream decisions. | Receiver can inspect CAF capabilities and send a device profile to Plex, but then we own format policy and support for each runtime. |
| Operations | Lowest additional operating burden; already used by this project | Existing PyChromecast dependency, but a different receiver backend and its compatibility still needs proof | Highest burden: Cast registration, hosted assets, authorization boundary, receiver updates, and device compatibility matrix |
| Google Assistant voice routing | Governed by existing Google Home integration and cloud traits | Unchanged by choosing the Plex receiver | Still unchanged by the custom Cast app alone; a receiver app ID is not a Google Assistant music-provider integration |

The current bridge queue implementation is visible in
[`speaker_queue.py`](../../src/plex_cast_bridge/speaker_queue.py): it owns the
full Python-side queue and schedules a single upcoming item into the receiver
queue. That is a useful resilience hint, but it is not evidence that a Cast
device will continue audibly after the bridge disconnects or restarts. The
single-item receiver lookahead is also not durable application queue storage.

## Jellyfin custom receiver research

The reviewed Jellyfin Cast Web Receiver repository is GPL-2.0 licensed; its
code was inspected as an architectural reference only. No Jellyfin code was
copied into this MIT project. Its contributor guide describes a separate
Custom Cast application, hosted receiver URL, Google Cast application ID, and
adding the app ID to the Jellyfin client/server configuration. ([Jellyfin Cast
repository](https://github.com/jellyfin/jellyfin-chromecast),
[CONTRIBUTING.md](https://github.com/jellyfin/jellyfin-chromecast/blob/master/CONTRIBUTING.md))

### Receiver-side design findings

- `maincontroller.ts` obtains CAF's `CastReceiverContext` and `PlayerManager`,
  installs playback event handlers, and treats `PLAYING`, `PAUSE`, media end,
  abort, and time updates as separate lifecycle events. It reports playback
  start only after `PLAYING`; it sends progress updates to the sender more
  frequently than to Jellyfin (about every 1.5 seconds to the message bus and
  every 5 seconds to the server). Stop reporting includes the final position.
  ([maincontroller.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/master/src/components/maincontroller.ts),
  [jellyfinActions.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/master/src/components/jellyfinActions.ts))
- `PlaybackManager` stores `activePlaylist` and `activePlaylistIndex` in static
  JavaScript memory, with next/previous/repeat selection. The `ENDED` event
  advances this receiver-side list without requiring a new sender command.
  The reviewed code has no durable queue checkpoint or restoration path, so
  this supports a source-level inference that track advancement can continue
  while the receiver remains alive, not a claim that it survives receiver or
  bridge restarts. The Cast Web Receiver lifecycle documentation notes that
  the application may end after the last sender disconnects. ([playbackManager.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/master/src/components/playbackManager.ts),
  [Cast Web Receiver basics](https://developers.google.com/cast/docs/web_receiver/basic))
- `commandHandler.ts` maps Cast/Jellyfin sender commands to receiver actions:
  play-next/now/last, shuffle, instant mix, next/previous, seek, pause/resume,
  stop, volume, and stream selection. This is an app-specific Cast control
  protocol; it is not Google Assistant provider registration.
  ([commandHandler.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/master/src/components/commandHandler.ts))
- `codecSupportHelper.ts` queries CAF `canDisplayType()` for runtime codec,
  profile, resolution, and bit-depth support. `deviceprofileBuilder.ts` turns
  detected capability into Jellyfin direct-play and transcoding profiles,
  including a reduced audio-only profile and a 192 kb/s music-transcode cap.
  The generic audio codec list includes Opus, Vorbis, MP3, AAC, FLAC, and WAV;
  the code's codec support is not a universal guarantee for every file
  container, sample rate, or Cast device. Actual audio-format behavior remains
  a hardware test. ([codecSupportHelper.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/master/src/components/codecSupportHelper.ts),
  [deviceprofileBuilder.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/master/src/components/deviceprofileBuilder.ts))
- Playback setup asks Jellyfin for `PlaybackInfo` with that device profile;
  Jellyfin returns media source, stream method, and session values which the
  receiver uses to construct playback. The reviewed `maincontroller.ts`,
  `playbackManager.ts`, `commandHandler.ts`, codec helper, and profile builder
  `maincontroller.ts` sets CAF `playbackConfig.autoResumeDuration = 5`; its
  comment says playback starts once five seconds of media is buffered (CAF's
  default is 10 seconds). CAF documents this setting as the buffered-media
  duration required to start or resume after buffering auto-pause. This is a
  playback buffer threshold, not proof of lower audible startup latency or a
  tunable queue depth; measure startup and recovery behavior on devices. The
  separate transcode ping is lifecycle management, not buffer-size control.
  ([maincontroller.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/c2677827f830d905b5dee95315928086a3b264af/src/components/maincontroller.ts),
  [CAF PlaybackConfig](https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.PlaybackConfig),
  [playbackManager.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/c2677827f830d905b5dee95315928086a3b264af/src/components/playbackManager.ts),
  [jellyfinActions.ts](https://github.com/jellyfin/jellyfin-chromecast/blob/c2677827f830d905b5dee95315928086a3b264af/src/components/jellyfinActions.ts))
- `jellyfinActions.ts` sends playback-start, progress, and stop records to
  Jellyfin and broadcasts corresponding state to connected senders. For
  transcoding it pings the play session during pauses so a server transcode
  does not expire. This is a strong pattern for accurate server history and
  progress in Jellyfin; Plex needs its own API and session behavior verified
  rather than copying Jellyfin endpoints or assuming equivalence.
- Google documents custom Web Receivers as Cast apps launched by a sender using
  a registered application ID. Audio-only support requires explicitly enabling
  it during registration and optimizing for audio-only memory, graphics, and
  volume constraints. Audio-only receivers support stream expansion by
  default, but that does not prove this custom app works with our Nest, a
  particular audio-only speaker, or Cast Group. ([Custom Web Receiver](https://developers.google.com/cast/docs/web_receiver/basic),
  [Cast audio devices](https://developers.google.com/cast/docs/audio),
  [Cast core features and stream transfer](https://developers.google.com/cast/docs/web_receiver/core_features))

### Custom receiver feasibility

This is technically feasible but is a separate product and operations project.
Google requires a Cast SDK Developer Console registration and app ID. An
unpublished receiver can be tested only on devices registered for development;
publishing makes the app available to Cast devices. Google currently documents
a non-refundable $5 developer registration fee. A published Custom Receiver
must be hosted at an HTTPS URL that the Cast device can reach. ([Google Cast
registration](https://developers.google.com/cast/docs/registration))

The receiver would also need a secure Plex authorization path. Jellyfin's
receiver expects server address and access token in its sender message, but
that pattern needs a separate security review before any reuse. A Plex design
would need either a tightly scoped, short-lived credential or a bridge-mediated
stream/control proxy, plus TLS, log redaction, receiver-to-Plex network
reachability, and revocation/expiry handling. The receiver must report
playback/session state itself if Plex is expected to maintain listening
history; a Cast app does not gain that behavior merely by playing a URL.

Distribution would require a maintained hosted receiver, registered app ID,
versioned deployments, a compatibility test matrix, and a launch path in the
bridge/sender. Development app access requires device registration; broad use
requires publication. Browser/receiver SDK and audio-only platform changes,
TLS certificates, app auth, playback controls, codec/transcode behavior, and
failure recovery become our maintenance burden. The receiver's media-state
reporting would not register a Google Assistant music provider, and it would
not make arbitrary spoken artist requests route to the bridge. This report
recommends **not** starting custom receiver implementation before the native
Plex experiment is completed.

### Improvements to consider without a custom receiver

- Keep the Default Media Receiver path and bridge-owned queue for now, but
  measure request-to-`PLAYING`, audible onset, and next-track latency as
  separate values; a controller response is not proof of sound.
- Preserve the current receiver-side one-track prequeue and test how far it
  carries playback after sender disconnection. If a future requirement is
  continuity through bridge restart, evaluate a durable queue checkpoint and
  safe resume/reconciliation using the existing `SpeakerQueue` generation and
  ownership protections.
- Use fresh Cast state and position samples for reporting; consider returning
  truthful media-state and position fields through Google Home QUERY traits.
  This may improve state awareness, but cannot itself add a Google music
  provider or prove arbitrary artist routing.
- Add measurement of source codec/container, Plex session stream decision,
  startup delay, and transition behavior for representative formats. Jellyfin's
  capability-profile model is useful research; CAF `canDisplayType()` exists
  inside a custom receiver and cannot simply be called by this Python bridge.
- Check whether Plex records sessions/history for current Default Media
  Receiver playback before designing new reporting. For a future native
  `PlexController` path, compare the Plex server PlayQueue/session directly
  against this baseline.

The current engine's bridge-managed queue, direct Plex-part URL and metadata
construction are visible in [speaker_queue.py](../../src/plex_cast_bridge/speaker_queue.py).
The native prototype is [native_plex_receiver_probe.py](native_plex_receiver_probe.py).

## Confirmed source and code findings

- Home Assistant's Plex Cast integration registers PyChromecast `PlexController`
  and passes Plex media to `play_media`; its Plex services resolve tracks,
  albums, artists, playlists, and station/PlayQueue requests. This is a
  technical precedent, not evidence that this standalone setup behaves the
  same on every Cast device. ([HA Plex Cast](https://github.com/home-assistant/core/blob/dev/homeassistant/components/plex/cast.py),
  [HA Plex services](https://github.com/home-assistant/core/blob/dev/homeassistant/components/plex/services.py),
  [HA Plex media search](https://github.com/home-assistant/core/blob/dev/homeassistant/components/plex/media_search.py),
  [HA Plex server](https://github.com/home-assistant/core/blob/dev/homeassistant/components/plex/server.py))
- Home Assistant's media search maps legacy criteria to Plex library searches,
  supports exact-title disambiguation, and rejects ambiguous matches unless
  multiple results are explicitly allowed. This is relevant to selection
  safety, while the probe itself requires one exact title.
- PyChromecast 14.1.1's `PlexController` uses app ID `9AC194DC`. Given a
  PlexAPI playable, its command builder creates a server PlayQueue when needed
  and includes Plex server/authentication fields in the Cast command. Those
  values must remain in memory and must not be logged. ([PyChromecast 14.1.1
  Plex controller](https://github.com/home-assistant-libs/pychromecast/blob/14.1.1/pychromecast/controllers/plex.py),
  [PyChromecast 14.1.1 package](https://pypi.org/project/PyChromecast/14.1.1/))
- Home Assistant uses `MultizoneManager`, records Cast Group membership and
  group media updates, and selects group status when an individual media
  status is absent or `UNKNOWN`. PyChromecast updates memberships from
  multizone status messages. This confirms group status is a distinct source
  of truth that an individual-only classifier can miss. HA's fallback does
  not replace an explicit individual `IDLE` status. Its discovery module
  converts PyChromecast `CastInfo` discoveries into Cast entities; the media
  player connection path creates the shared multizone manager. This does not
  prove what this Nest reports during an active group session. ([HA Cast discovery](https://github.com/home-assistant/core/blob/dev/homeassistant/components/cast/discovery.py),
  [HA Cast media player](https://github.com/home-assistant/core/blob/dev/homeassistant/components/cast/media_player.py),
  [PyChromecast multizone controller](https://github.com/home-assistant-libs/pychromecast/blob/14.1.1/pychromecast/controllers/multizone.py))
- `inspect_cast_session()` in
  [voice_cold_start.py](../../src/plex_cast_bridge/voice_cold_start.py)
  checks connected state, fresh receiver/media callbacks, media session,
  receiver app, and bridge-owned queue state. It does not ask a multizone
  manager about group membership. If the member supplies a valid empty
  Default Media Receiver session and standby status while its group is
  playing, the current code has no group evidence with which to reject the
  `idle` classification. That conditional is a code-level safety gap, not yet
  a confirmed hardware failure. An offline regression supplies a separate
  active-group observation alongside a fresh individual standby/empty-media
  response and confirms the classifier still returns `idle`; this verifies
  the missing input in code, not the Nest's actual group reporting behavior.
- The experiment script's status gate only accepts successful, well-formed
  Cast responses. It requires a complete scan of discovered Cast Groups and
  positive `MULTIZONE_STATUS` from every discovered group; unknown or failed
  group observations cannot authorize playback. Offline tests cover those
  gates. mDNS visibility is still a limit: an empty scan means no groups were
  observed in that scan, not proof that group discovery is complete across
  every network condition.
- Google Home trait comparison:

  | Trait | Home Assistant | Existing bridge gateway |
  | --- | --- | --- |
  | MediaState | Advertises activity and playback state; QUERY maps entity states such as playing/paused/idle into `activityState` and `playbackState`. | Advertises the trait but sets both support flags false; QUERY deliberately omits playback state. |
  | OnOff | QUERY reports `on` from entity state; assumed state can advertise command-only behavior. | `commandOnlyOnOff` is true; QUERY returns availability/status but no `on` value. |
  | Volume | Advertises volume/mute capabilities from player features and can report current volume/mute. | `commandOnlyVolume` is true; QUERY does not return current level. |
  | TransportControl | Advertises supported commands based on player features, including next/previous/pause/resume/stop and optionally seek/shuffle. | Advertises next/previous/pause/resume/stop. |

  Accurate MediaState reporting could give Google a better active/paused/idle
  signal for command routing. That is a hypothesis, not a demonstrated fix.
  MediaState itself carries no arbitrary artist query. The gateway separately
  exposes catalog-derived channels/apps, but earlier voice trials did not
  consistently reach fulfillment; they do not prove that arbitrary artist
  requests will reach `selectChannel` or Plex search. See the earlier
  [voice cold-start report](voice_cold_start_report.md). ([Bridge gateway](../../gcp-cloud-run/src/cloud_gateway/smart_home.py),
  [HA Google Assistant traits](https://github.com/home-assistant/core/blob/dev/homeassistant/components/google_assistant/trait.py))

## Existing-engine hardware evidence

The prior voice-cold-start experiment used the existing Default Media Receiver
path, not native Plex. It recorded audible playback of a configured Plex
selection with advancing Cast position, and resume of an existing paused Plex
session without replacing that queue. It also recorded inconsistent Google
dispatch for “play music on …” and arbitrary artist phrasing. Those results are
the baseline only; none establish native Plex playback. See
[voice_cold_start_report.md](voice_cold_start_report.md).

## Latest read-only native probe

The probe ran by streaming the script to `python -` inside the existing
`plex-cast-bridge` container. It did not modify the image, persistent data,
catalog, cloud service, or Google Home definitions, and it did not issue a
playback command.

| Observation | Result |
| --- | --- |
| Runtime | PyChromecast 14.1.1 |
| Target identity | A Google Nest display; physical Cast identity independently verified through DIAL |
| Cast socket | Connected |
| Receiver status | Callback returned `RECEIVER_STATUS`, but required fresh standby/application fields did not validate |
| Media status | Media namespace not active; no fresh media status was available |
| Receiver volume | Fresh Cast volume response reported 0.67; no volume change was made |
| Group scan | 0 Cast Groups observed; no membership rows; this is not a hardware group-playback test |
| Cold-start classification | `indeterminate`; no playback allowed |
| Plex Music inventory | Read-only inventory query completed; personal catalog counts and contents omitted |

The user reported the display was in use or could not be checked for silence.
The test therefore correctly stopped before playback. The absence of validated
receiver/media state is not evidence that the receiver was idle.

## Native hardware matrix

Every native playback case remains **not run**. Results require a confirmed
idle receiver, reduced volume, a successful Cast media-session observation,
advancing position, and the user's audible confirmation. Do not count a
controller call or verbal Google acknowledgement as playback proof.

| Case | Result | Required evidence |
| --- | --- | --- |
| Cold start from idle | Not run | Fresh default/idle receiver and media status; app launch; advancing position; audible confirmation |
| Single track | Not run | Exact known track; Plex session; media status/position; audible confirmation |
| Album | Not run | Plex queue size and ordered track changes |
| Artist shuffle | Not run | Multiple distinct selected tracks and successful transitions |
| Plex server-managed queue | Not run | Queue continuity and same Plex session across next/previous |
| Existing Plex playlist | Not run | Audio playlist selection and ordered playback |
| Plex station | Not run | Station support and returned queue, or a clear supported/unsupported result |
| Pause/resume | Not run | Same session and track paused/resumed, with position advancing after resume |
| Next/previous | Not run | Track changes and measured transition latency |
| Queue continuity | Not run | Current and next item remain in the Plex server-managed queue |
| Transition latency | Not run | Timestamp from command to fresh changed-track `PLAYING` status |
| Gapless playback | Not run | Suitable adjacent material and audio capture/listening test; Cast status alone is insufficient |
| Direct/transcode | Not run | Plex session's stream decisions and server activity during playback |
| Listening history/session | Not run | Plex session while playing, stream decisions/progress, and history after completion |
| Cast metadata/artwork | Not run | Native receiver metadata/artwork visible on target and reported by Cast |
| Speaker compatibility | Not run | Confirm on Nest Hub; then other speaker/Chromecast models if available |
| Cast Group compatibility | Not run | Confirm membership/status, group playback, and audible output without changing group membership |
| Disconnect/restart recovery | Not run | Stop/restore protocol, controlled reconnect, session and queue behavior |
| Originating sender disconnect | Not run | Disconnect only the controlling sender after a multi-track queue starts; record whether current and subsequent tracks continue audibly |
| Bridge restart and receiver queue | Not run | On a disposable non-live instance, compare current track, receiver-preloaded item, and remaining queue before/after process restart; record reconnection and queue recovery |
| Request-to-audible latency | Not run | Timestamp command, first fresh `PLAYING`, and user-observed audible onset separately |
| Audio format direct/transcode matrix | Not run | For representative MP3, AAC/M4A, FLAC, WAV, and other available formats, capture PMS session, stream decision, and audible behavior |
| State and position reporting | Not run | Compare fresh Cast state/position against audible playback, pauses, track changes, and PMS session state |
| Audio-only device compatibility | Not run | Test a registered audio-only Cast device; confirm discovery, launch, controls, metadata, volume safety, and audible output |
| Volume safety | Not run | Verify reduced level before start and restore only after stop is positively observed |

All additional Jellyfin-inspired hardware checks in this table are deliberately
tabled at the user's request. Source inspection and offline test results do
not substitute for these device observations. The existing constraint against
changing the live Docker installation rules out restarting that container for
the queue-survival test; a disposable clone is required for a true bridge
process-restart comparison.

## Probe safeguards and verification

- Playback requires `--allow-disruptive`, an exact configured UUID, independent
  DIAL identity for individual speakers (or exact fresh mDNS group identity),
  and positive idle/group checks. `--confirm-physical-idle` is only for an
  in-person confirmation when Cast status remains `indeterminate`. It cannot
  override a media session, known active state, another receiver app, failed
  group discovery, or a member's known group membership.
- Volume is reduced to the minimum of the original level, 0.12, and the
  configured maximum, based on a fresh receiver volume response. It is restored
  only after stop is positively observed; otherwise it remains at the reduced
  level.
- The CLI supports `--engine native` (default) and `--engine bridge` so the
  same exact selection can be exercised through PyChromecast `PlexController`
  and the existing `SpeakerQueue`. Playlist and station selections are
  materialized as track lists when run through the bridge engine; their queue
  semantics may therefore differ from Plex's native PlayQueue.
- No command payload, Plex URL, access token, JWT, or exception message is
  printed. Session output is restricted to player/product/state/title/offset
  and stream-decision values.
- Validation run locally: `py_compile` passed, Ruff passed,
  `tests/test_native_plex_receiver_probe.py` passed (13 tests). The supported
  targeted voice and probe tests passed (44 total), including the separate
  group-gap regression. The supported offline suite passed (636 passed, 30
  Docker integration tests deselected). These are code and safety checks, not
  playback evidence. Position advancement is recorded only from fresh
  `PLAYING` status responses; stale cached position is not counted. The probe
  explicitly leaves audible confirmation as a required in-person observation.

## Recommendation and deferred hardware validation

Do not replace the Default Media Receiver or enable native Plex in production.
Keep native Plex as an experimental optional-backend candidate only. The
potential benefit is Plex-owned queue continuity and receiver-side Plex
metadata; the unknowns include Nest compatibility, audio stream negotiation,
groups, history, and recovery.

Hardware validation is tabled. When it is resumed, start with a known
in-library single track on a device confirmed silent and idle, at reduced test
volume. Record fresh `PLAYING`, position advancement, audible onset, and Plex
session evidence, then positively verify stop before restoring volume. Compare
the same selection through the Default Media Receiver and native Plex receiver
before expanding to albums, playlists, stations, transport, format, sender
disconnect, restart, audio-only, group, and recovery cases.
