# Voice Cold Start

Voice Cold Start lets a Google Home `resume` command start a configured Plex
selection when its Cast destination is confirmed idle. It is an opt-in setting
for each speaker and is off by default, including newly discovered speakers.
It uses the existing Google Home → Cloud Run → Pub/Sub → bridge → Cast path;
it requires no additional GCP resources, credentials, services, or containers.

## Enable and configure

Open **Speakers**, find the destination, enable **Voice Cold Start**, then choose
**Shuffle music library** or **Preferred artist**. For the artist option, enter
an artist in the connected Plex music library. The shuffle selection asks Plex
for at most 25 randomly ordered tracks, so it does not download the whole
library. Playback goes through the existing Cast queue and volume safety cap.

Settings are stored with the speaker's stable Google Home device ID in the
existing speaker catalog. They survive bridge restarts, image upgrades,
rediscovery, speaker renames and IP changes, and are included in the existing
catalog backup and restore.

## Voice behavior and limits

The experimentally verified phrasing was **“Hey Google, resume Master Bedroom
Plex”** and **“Hey Google, start Master Bedroom Plex.”** Start is currently an
existing Google Home command; cold start is deliberately attached only to the
existing `resume` command. Other phrasing, including arbitrary spoken artist
searches and “play music on …”, was inconsistent in the hardware experiment.
Google may not route those requests to this Plex device. Plex Cast Bridge is
not a native Google music provider.

An existing paused Plex queue resumes without replacement. Active Plex media
is preserved. A session from another app, disconnected receiver, or uncertain
receiver state is not replaced. Cold start only runs when fresh receiver and
media status confirms the Default Media Receiver is in standby with no media
session and the bridge queue is empty. The bridge also checks maintained Cast
Group membership and group media status. Playing or paused group playback
blocks a new selection even if the individual receiver reports idle. Missing,
stale, disconnected, or incomplete group state is treated as uncertain and
blocks cold start. An empty discovery result alone does not establish that no
Cast Groups exist. The no-group case requires two stable, nonempty periodic Cast
discoveries before it can be considered clear.

A connected receiver that does not advertise the Google Cast media namespace
remains uncertain. The bridge does not launch a receiver to inspect it, and an
inactive namespace alone does not prove that no media application is active.

## Troubleshooting

- Confirm the speaker is enabled and visible to Google Home, then refresh Google
  Home synchronization if its virtual device name changed.
- Confirm **Voice Cold Start** is enabled and the Plex selection exists. A
  missing artist or empty library fails without substituting unrelated music.
- If the receiver is disconnected or its fresh status cannot be confirmed, the
  bridge skips the fallback. Restore Cast connectivity and try again.
- If Google answers that it cannot control media or does not recognize the
  device, the request may not have reached bridge fulfillment. This feature
  cannot correct Google's speech routing.
- Inspect sanitized bridge logs for `Voice cold start` outcomes. The logs do
  not include voice text, Plex URLs, or authentication tokens.
