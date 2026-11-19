"""Stream watcher: sends finished live streams of chosen YouTube channels to
OpenShorts, so long VODs become clips without anyone pasting URLs.

Every INTERVAL seconds it lists each channel's /streams tab with yt-dlp (flat,
nothing is downloaded), picks the streams that have ended (``was_live``) inside
a duration band, and submits each one to OpenShorts' ``POST /api/process``.
Whole VODs are sent, not chunks: OpenShorts scores the full transcript and
keeps the best moments of the whole stream.

Guard rails:
- The first look at a channel records its existing streams as "skipped", so
  turning the watcher on never clips the back catalogue.
- ``max_jobs_per_day`` caps submissions in any rolling 24 h (Gemini spend).
- ``max_in_flight`` (default 1) is how many of its jobs may run at once: the
  next stream waits until the previous job has completed or failed.
- A submission that fails (backend down) is retried on the next run; a job
  that fails in OpenShorts is recorded and never resubmitted automatically.
- It refuses to start unless the config says ``acknowledge_rights: true``:
  OpenShorts asks every job to confirm the submitter has the rights to it.

The decision logic (``run_once``) takes the listing, submit and status calls as
functions, so it is tested without network (tests/test_watcher.py).
"""
import argparse
import json
import logging
import os
import sys
import tempfile
import time

log = logging.getLogger("watcher")

DAY = 24 * 3600
DEFAULTS = {
    "layouts": ["auto"],
    "auto_hook": True,
    "auto_hook_style": None,
    "target_clips": None,
    # Clip length band in seconds; None keeps OpenShorts' own (15-60 s).
    "clip_min_seconds": None,
    "clip_max_seconds": None,
    "min_minutes": 20,
    "max_hours": 6,
}
# OpenShorts statuses that end a job.
FINAL_STATUSES = ("completed", "failed")


class ConfigError(Exception):
    pass


class SubmitError(Exception):
    pass


# --- config ------------------------------------------------------------------

def _streams_url(url):
    url = str(url).strip().rstrip("/")
    if not url.endswith("/streams"):
        url += "/streams"
    return url


# The bounds POST /api/process enforces (app.process_endpoint). Checked here so
# a bad value stops the watcher with a clear message instead of every
# submission coming back 400 and being retried run after run.
CLIP_MIN_RANGE = (5, 175)
CLIP_MAX_RANGE = (10, 180)
CLIP_MIN_GAP = 5


def _check_clip_lengths(options, where):
    values = {}
    for name, (lo, hi) in (("clip_min_seconds", CLIP_MIN_RANGE),
                           ("clip_max_seconds", CLIP_MAX_RANGE)):
        raw = options.get(name)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: {name} must be a number (got {raw!r})")
        if not lo <= value <= hi:
            raise ConfigError(f"{where}: {name} must be between {lo} and {hi} seconds")
        values[name] = value
    lo, hi = values.get("clip_min_seconds"), values.get("clip_max_seconds")
    if lo is not None and hi is not None and hi < lo + CLIP_MIN_GAP:
        raise ConfigError(f"{where}: clip_max_seconds must be at least {CLIP_MIN_GAP} s "
                          "above clip_min_seconds")


def normalize_config(raw):
    """Validate the parsed channels file and resolve per-channel options."""
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a mapping")
    if raw.get("acknowledge_rights") is not True:
        raise ConfigError(
            "set acknowledge_rights: true to confirm you have the rights to process "
            "these channels' streams (OpenShorts records this for every job)")
    channels = raw.get("channels") or []
    if not channels:
        raise ConfigError("channels: list at least one channel URL")
    defaults = {**DEFAULTS, **(raw.get("defaults") or {})}
    resolved = []
    for ch in channels:
        if isinstance(ch, str):
            ch = {"url": ch}
        if not isinstance(ch, dict) or not ch.get("url"):
            raise ConfigError(f"channels: each entry needs a url (got {ch!r})")
        options = {**defaults, **{k: v for k, v in ch.items() if k != "url"}}
        _check_clip_lengths(options, ch["url"])
        resolved.append({"url": ch["url"], "streams_url": _streams_url(ch["url"]),
                         "options": options})
    return {"max_jobs_per_day": int(raw.get("max_jobs_per_day", 3)),
            "max_in_flight": max(1, int(raw.get("max_in_flight", 1))),
            "channels": resolved}


def load_config(path):
    import yaml  # lazy: the tests run without PyYAML
    with open(path, encoding="utf-8") as f:
        return normalize_config(yaml.safe_load(f))


# --- state -------------------------------------------------------------------

def empty_state():
    return {"channels": {}, "videos": {}}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty_state()
    state = empty_state()
    state.update({k: data.get(k) or {} for k in state})
    return state


def save_state(path, state):
    """Atomic write: a crash mid-save never leaves a truncated state file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --- decisions -----------------------------------------------------------------

def select_new_streams(entries, seen, min_seconds, max_seconds):
    """Finished streams not seen before whose length is inside the band."""
    picked = []
    for e in entries:
        if e.get("live_status") != "was_live" or e.get("id") in seen:
            continue
        duration = e.get("duration") or 0
        if min_seconds <= duration <= max_seconds:
            picked.append(e)
    return picked


def _submitted_since(state, since):
    return sum(1 for v in state["videos"].values()
               if v.get("submitted_at") and v["submitted_at"] >= since)


def _job_options(options):
    body = {"acknowledged": True, "layouts": list(options.get("layouts") or [])}
    if options.get("auto_hook"):
        body["auto_hook"] = True
        if options.get("auto_hook_style"):
            body["auto_hook_style"] = options["auto_hook_style"]
    if options.get("target_clips"):
        body["target_clips"] = int(options["target_clips"])
    for name in ("clip_min_seconds", "clip_max_seconds"):
        if options.get(name) not in (None, ""):
            body[name] = float(options[name])
    return body


def _refresh_jobs(state, status_fn, now):
    for vid, v in state["videos"].items():
        if v.get("status") != "submitted":
            continue
        try:
            status = status_fn(v["job_id"])
        except Exception as e:  # noqa: BLE001 - a poll failure is retried next run
            log.warning("could not read job %s of %s: %s", v["job_id"], vid, e)
            continue
        if status is None:
            # 404: the backend no longer has it (swept, or its folder deleted).
            # An interrupted job resumes from its manifest and still answers,
            # so this is not "still running": free the slot.
            status = "lost"
        if status in FINAL_STATUSES + ("lost",):
            v["status"] = status
            v["finished_at"] = now
            log.info("%s %s: %s (job %s)", status.upper(), vid, v.get("title"), v["job_id"])


def run_once(config, state, list_fn, submit_fn, status_fn, now):
    """One pass: update finished jobs, then look for new streams."""
    _refresh_jobs(state, status_fn, now)
    in_flight = sum(1 for v in state["videos"].values() if v.get("status") == "submitted")
    budget = min(config["max_jobs_per_day"] - _submitted_since(state, now - DAY),
                 config["max_in_flight"] - in_flight)
    for ch in config["channels"]:
        url = ch["streams_url"]
        try:
            entries = list_fn(url) or []
        except Exception as e:  # noqa: BLE001 - one bad channel must not stop the rest
            log.warning("could not list %s: %s", url, e)
            continue
        if url not in state["channels"]:
            # First look: the backlog is history, not work.
            for e in entries:
                if e.get("id") and e["id"] not in state["videos"]:
                    state["videos"][e["id"]] = {"status": "skipped", "title": e.get("title")}
            state["channels"][url] = {"added_at": now}
            log.info("watching %s (%d existing streams left alone)", url, len(entries))
            continue
        opts = ch["options"]
        fresh = select_new_streams(entries, set(state["videos"]),
                                   float(opts["min_minutes"]) * 60,
                                   float(opts["max_hours"]) * 3600)
        for e in reversed(fresh):  # oldest first
            if budget <= 0:
                log.info("daily cap or a job still running; %s waits for a later run", e["id"])
                break
            video_url = f"https://www.youtube.com/watch?v={e['id']}"
            try:
                job_id = submit_fn(video_url, _job_options(opts))
            except SubmitError as err:
                log.warning("could not submit %s: %s (retrying next run)", e["id"], err)
                continue
            budget -= 1
            state["videos"][e["id"]] = {
                "status": "submitted", "job_id": job_id, "title": e.get("title"),
                "channel": url, "submitted_at": now,
            }
            log.info("SUBMITTED %s: %s -> job %s", e["id"], e.get("title"), job_id)
    return state


# --- I/O -----------------------------------------------------------------------

def list_streams(url, limit=15):
    import yt_dlp
    opts = {"extract_flat": "in_playlist", "playlistend": limit,
            "quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return list(info.get("entries") or [])


def make_backend(base_url, gemini_key=None, timeout=60):
    import httpx
    headers = {"X-Gemini-Key": gemini_key} if gemini_key else {}

    def submit(video_url, options):
        try:
            r = httpx.post(f"{base_url}/api/process", json={"url": video_url, **options},
                           headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            raise SubmitError(str(e)) from e
        if r.status_code >= 400:
            raise SubmitError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["job_id"]

    def status(job_id):
        r = httpx.get(f"{base_url}/api/status/{job_id}", headers=headers, timeout=timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("status")

    return submit, status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("WATCHER_CONFIG", "/config/channels.yml"))
    parser.add_argument("--state", default=os.environ.get("WATCHER_STATE", "/data/state.json"))
    parser.add_argument("--backend", default=os.environ.get("OPENSHORTS_URL", "http://backend:8000"))
    parser.add_argument("--interval", type=int,
                        default=int(os.environ.get("WATCHER_INTERVAL_SECONDS", "1800")))
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll is noise

    submit, status = make_backend(args.backend.rstrip("/"), os.environ.get("GEMINI_API_KEY"))
    while True:
        try:
            config = load_config(args.config)
        except (OSError, ConfigError) as e:
            log.error("config %s: %s", args.config, e)
            if args.once:
                return 2
        else:
            state = load_state(args.state)
            run_once(config, state, list_streams, submit, status, time.time())
            save_state(args.state, state)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
