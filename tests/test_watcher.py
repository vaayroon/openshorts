"""watcher/watcher.py: the stream watcher that feeds finished live streams to
OpenShorts. Only the decision logic is tested here; yt-dlp and HTTP are passed
in as functions, so no network is touched."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
import watcher  # noqa: E402

HOUR = 3600
NOW = 1_800_000_000.0


def _entry(vid, status="was_live", duration=3 * HOUR, title=None):
    return {"id": vid, "live_status": status, "duration": duration,
            "title": title or f"Stream {vid}"}


def _config(**overrides):
    cfg = {
        "acknowledge_rights": True,
        "max_jobs_per_day": 5,
        "channels": [{"url": "https://www.youtube.com/@someone"}],
    }
    cfg.update(overrides)
    return watcher.normalize_config(cfg)


class Recorder:
    """Stands in for OpenShorts: records submissions, answers statuses."""

    def __init__(self, statuses=None, fail_submit=False):
        self.submitted = []
        self.statuses = statuses or {}
        self.fail_submit = fail_submit

    def submit(self, url, options):
        if self.fail_submit:
            raise watcher.SubmitError("backend unreachable")
        self.submitted.append((url, options))
        return f"job-{len(self.submitted)}"

    def status(self, job_id):
        return self.statuses.get(job_id, "processing")


class TestSelectNewStreams:
    def test_keeps_only_finished_streams_inside_the_duration_band(self):
        entries = [
            _entry("upcoming", status="is_upcoming", duration=None),
            _entry("live", status="is_live", duration=None),
            _entry("short", duration=10 * 60),
            _entry("long", duration=9 * HOUR),
            _entry("good", duration=3 * HOUR),
            _entry("upload", status="not_live", duration=3 * HOUR),
        ]
        picked = watcher.select_new_streams(entries, seen=set(),
                                            min_seconds=20 * 60, max_seconds=6 * HOUR)
        assert [e["id"] for e in picked] == ["good"]

    def test_skips_streams_already_seen(self):
        entries = [_entry("a"), _entry("b")]
        picked = watcher.select_new_streams(entries, seen={"a"},
                                            min_seconds=0, max_seconds=10 * HOUR)
        assert [e["id"] for e in picked] == ["b"]


class TestRunOnce:
    def test_first_look_at_a_channel_records_its_backlog_without_submitting(self):
        state = watcher.empty_state()
        backend = Recorder()
        listing = {"https://www.youtube.com/@someone/streams": [_entry("old1"), _entry("old2")]}

        watcher.run_once(_config(), state, listing.get, backend.submit, backend.status, NOW)

        assert backend.submitted == []
        assert state["videos"]["old1"]["status"] == "skipped"
        assert state["videos"]["old2"]["status"] == "skipped"

    def test_a_new_finished_stream_is_submitted_with_the_channel_options(self):
        state = watcher.empty_state()
        backend = Recorder()
        cfg = _config(defaults={"layouts": ["screencast"], "auto_hook_style": "outline",
                                "target_clips": 8})
        url = "https://www.youtube.com/@someone/streams"
        watcher.run_once(cfg, state, {url: [_entry("old")]}.get, backend.submit, backend.status, NOW)

        watcher.run_once(cfg, state, {url: [_entry("new"), _entry("old")]}.get,
                         backend.submit, backend.status, NOW + 60)

        assert len(backend.submitted) == 1
        submitted_url, options = backend.submitted[0]
        assert submitted_url == "https://www.youtube.com/watch?v=new"
        assert options["acknowledged"] is True
        assert options["layouts"] == ["screencast"]
        assert options["auto_hook"] is True
        assert options["auto_hook_style"] == "outline"
        assert options["target_clips"] == 8
        assert state["videos"]["new"]["status"] == "submitted"
        assert state["videos"]["new"]["job_id"] == "job-1"

    def test_a_failed_submission_is_retried_on_the_next_run(self):
        state = watcher.empty_state()
        url = "https://www.youtube.com/@someone/streams"
        watcher.run_once(_config(), state, {url: []}.get, Recorder().submit, Recorder().status, NOW)
        down = Recorder(fail_submit=True)

        watcher.run_once(_config(), state, {url: [_entry("new")]}.get, down.submit, down.status, NOW)
        assert "new" not in state["videos"]

        up = Recorder()
        watcher.run_once(_config(), state, {url: [_entry("new")]}.get, up.submit, up.status, NOW + 60)
        assert [u for u, _ in up.submitted] == ["https://www.youtube.com/watch?v=new"]

    def test_the_daily_cap_holds_the_rest_for_later(self):
        state = watcher.empty_state()
        url = "https://www.youtube.com/@someone/streams"
        cfg = _config(max_jobs_per_day=1, max_in_flight=5)
        backend = Recorder()
        watcher.run_once(cfg, state, {url: []}.get, backend.submit, backend.status, NOW)

        watcher.run_once(cfg, state, {url: [_entry("a"), _entry("b")]}.get,
                         backend.submit, backend.status, NOW)
        assert len(backend.submitted) == 1
        assert len([v for v in state["videos"].values() if v["status"] == "submitted"]) == 1

        watcher.run_once(cfg, state, {url: [_entry("a"), _entry("b")]}.get,
                         backend.submit, backend.status, NOW + 25 * HOUR)
        assert len(backend.submitted) == 2

    def test_only_one_job_runs_at_a_time_by_default(self):
        state = watcher.empty_state()
        url = "https://www.youtube.com/@someone/streams"
        backend = Recorder()
        listing = {url: [_entry("a"), _entry("b")]}.get
        watcher.run_once(_config(), state, {url: []}.get, backend.submit, backend.status, NOW)

        watcher.run_once(_config(), state, listing, backend.submit, backend.status, NOW)
        assert len(backend.submitted) == 1

        watcher.run_once(_config(), state, listing, backend.submit, backend.status, NOW + 60)
        assert len(backend.submitted) == 1, "the first job is still processing"

        backend.statuses = {"job-1": "completed"}
        watcher.run_once(_config(), state, listing, backend.submit, backend.status, NOW + 120)
        assert len(backend.submitted) == 2

    def test_a_job_the_backend_no_longer_knows_frees_its_slot(self):
        state = watcher.empty_state()
        url = "https://www.youtube.com/@someone/streams"
        backend = Recorder()
        listing = {url: [_entry("a"), _entry("b")]}.get
        watcher.run_once(_config(), state, {url: []}.get, backend.submit, backend.status, NOW)
        watcher.run_once(_config(), state, listing, backend.submit, backend.status, NOW)
        backend.status = lambda job_id: None  # 404: swept or lost

        watcher.run_once(_config(), state, listing, backend.submit, backend.status, NOW + 60)

        assert len(backend.submitted) == 2
        assert [v["status"] for v in state["videos"].values()].count("lost") == 1

    def test_job_outcomes_are_recorded_and_a_failed_job_is_not_resubmitted(self):
        state = watcher.empty_state()
        url = "https://www.youtube.com/@someone/streams"
        backend = Recorder()
        cfg = _config(max_in_flight=2)
        watcher.run_once(cfg, state, {url: []}.get, backend.submit, backend.status, NOW)
        watcher.run_once(cfg, state, {url: [_entry("ok"), _entry("bad")]}.get,
                         backend.submit, backend.status, NOW)
        jobs = {v["job_id"]: vid for vid, v in state["videos"].items() if v.get("job_id")}
        backend.statuses = {j: ("completed" if vid == "ok" else "failed") for j, vid in jobs.items()}

        watcher.run_once(cfg, state, {url: [_entry("ok"), _entry("bad")]}.get,
                         backend.submit, backend.status, NOW + 60)

        assert state["videos"]["ok"]["status"] == "completed"
        assert state["videos"]["bad"]["status"] == "failed"
        assert len(backend.submitted) == 2

    def test_a_channel_that_cannot_be_listed_does_not_stop_the_others(self):
        state = watcher.empty_state()
        cfg = _config(channels=[{"url": "https://www.youtube.com/@broken"},
                                {"url": "https://www.youtube.com/@fine"}])

        def listing(url):
            if "broken" in url:
                raise RuntimeError("yt-dlp failed")
            return [_entry("x")]

        watcher.run_once(cfg, state, listing, Recorder().submit, Recorder().status, NOW)

        assert state["videos"]["x"]["status"] == "skipped"
        assert "https://www.youtube.com/@fine/streams" in state["channels"]
        assert "https://www.youtube.com/@broken/streams" not in state["channels"]


class TestConfig:
    def test_refuses_to_run_without_the_rights_acknowledgement(self):
        with pytest.raises(watcher.ConfigError, match="acknowledge_rights"):
            watcher.normalize_config({"channels": [{"url": "https://www.youtube.com/@x"}]})

    def test_needs_at_least_one_channel(self):
        with pytest.raises(watcher.ConfigError, match="channels"):
            watcher.normalize_config({"acknowledge_rights": True, "channels": []})

    def test_a_channel_url_points_at_its_streams_tab(self):
        cfg = _config(channels=[{"url": "https://www.youtube.com/@a"},
                                {"url": "https://www.youtube.com/@b/streams/"}])
        assert [c["streams_url"] for c in cfg["channels"]] == [
            "https://www.youtube.com/@a/streams", "https://www.youtube.com/@b/streams"]

    def test_channel_options_override_the_defaults(self):
        cfg = _config(defaults={"layouts": ["auto"], "target_clips": 5},
                      channels=[{"url": "https://www.youtube.com/@a", "target_clips": 10}])
        assert cfg["channels"][0]["options"]["target_clips"] == 10
        assert cfg["channels"][0]["options"]["layouts"] == ["auto"]


class TestStateFile:
    def test_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "state.json"
        state = watcher.empty_state()
        state["videos"]["a"] = {"status": "skipped"}
        watcher.save_state(str(path), state)
        assert watcher.load_state(str(path)) == state

    def test_a_missing_file_is_an_empty_state(self, tmp_path):
        assert watcher.load_state(str(tmp_path / "nope.json")) == watcher.empty_state()
