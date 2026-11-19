# Stream watcher

Sends the finished live streams of chosen YouTube channels to OpenShorts, so a
3-4 hour VOD becomes clips without pasting URLs. The whole VOD is sent, not
chunks: OpenShorts scores the full transcript and keeps the best moments of the
entire stream (see `docs/architecture/clip-selection.md`).

Finished jobs show up under **Recent projects** in the dashboard, where you
review and publish them. Nothing is posted automatically.

## Setup

1. Create your channel list and set `acknowledge_rights: true` once you have
   the rights to process those channels' streams:

   ```bash
   cp watcher/channels.example.yml watcher/channels.yml
   ```

2. Create the watcher's env file and put your Gemini key in it (the key you
   saved in the dashboard lives in your browser, which the watcher cannot
   reach):

   ```bash
   cp watcher/.env.example watcher/.env
   ```

3. Add the watcher to the stack in the root `.env` (Compose reads
   `COMPOSE_FILE` only from there):

   ```
   COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.watcher.yml
   ```

4. Start it:

   ```bash
   docker compose up -d --build watcher
   docker logs -f openshorts-watcher
   ```

## Behaviour

- Every `WATCHER_INTERVAL_SECONDS` (default 1800) it lists each channel's
  `/streams` tab with yt-dlp (nothing is downloaded) and submits streams that
  have ended (`was_live`) and last between `min_minutes` and `max_hours`.
- The first look at a channel leaves its existing streams alone: only streams
  that end after you add a channel are clipped.
- `max_jobs_per_day` caps submissions in any rolling 24 hours; the rest wait.
- `max_in_flight` (default 1): the next stream is submitted only once the
  previous watcher job has completed or failed. Jobs you start by hand in the
  dashboard are not counted.
- A submission that fails (backend down, missing key) is retried next run. A
  job that fails inside OpenShorts is logged and not resubmitted.
- `channels.yml` is re-read on every run: edits apply without a restart.
- State (what was seen, submitted, finished) lives in the `watcher-data`
  volume. To inspect it:

  ```bash
  docker exec openshorts-watcher cat /data/state.json
  ```

Streams still live are never submitted: OpenShorts needs the finished VOD.
