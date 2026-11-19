"""GET /api/recent-jobs: the self-host list of finished jobs to reopen.

Self-host has no History (that is the Cloud library), so after a reload that
dropped the saved session the only way back to a job was pasting its id into
localStorage. This endpoint lists what is still on disk instead.
"""
import asyncio
import os
import time

import httpx

import app as app_module
from app import app


def _get(path):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get(path)
    return asyncio.run(_run())


def _job_dir(tmp_path, job_id, title, mtime):
    path = tmp_path / job_id
    path.mkdir()
    meta = path / f"{job_id}_{title}_metadata.json"
    meta.write_text("{}")
    os.utime(meta, (mtime, mtime))
    return str(path)


def _completed(output_dir, n_clips):
    clips = [{"video_url": f"/videos/x/c{i}.mp4"} for i in range(n_clips)]
    return {"status": "completed", "logs": [], "output_dir": output_dir,
            "result": {"clips": clips}}


class TestRecentJobs:
    def setup_method(self):
        self._saved = dict(app_module.jobs)
        app_module.jobs.clear()

    def teardown_method(self):
        app_module.jobs.clear()
        app_module.jobs.update(self._saved)

    def test_lists_finished_jobs_newest_first_with_title_and_clip_count(self, tmp_path):
        now = time.time()
        old = "11111111-1111-4111-8111-111111111111"
        new = "22222222-2222-4222-8222-222222222222"
        app_module.jobs[old] = _completed(_job_dir(tmp_path, old, "Old video", now - 3600), 2)
        app_module.jobs[new] = _completed(_job_dir(tmp_path, new, "GTA 6 - Mundo abierto", now), 6)

        resp = _get("/api/recent-jobs")

        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert [j["job_id"] for j in jobs] == [new, old]
        assert jobs[0]["title"] == "GTA 6 - Mundo abierto"
        assert jobs[0]["clip_count"] == 6
        assert jobs[1]["clip_count"] == 2
        assert jobs[0]["finished_at"] == int(now)

    def test_leaves_out_jobs_that_are_not_finished_or_have_no_clips(self, tmp_path):
        done = "33333333-3333-4333-8333-333333333333"
        app_module.jobs[done] = _completed(_job_dir(tmp_path, done, "Done", time.time()), 1)
        empty = "44444444-4444-4444-8444-444444444444"
        app_module.jobs[empty] = _completed(_job_dir(tmp_path, empty, "Empty", time.time()), 0)
        app_module.jobs["55555555-5555-4555-8555-555555555555"] = {
            "status": "processing", "logs": [], "result": None}
        app_module.jobs["66666666-6666-4666-8666-666666666666"] = {
            "status": "failed", "logs": [], "result": None}

        jobs = _get("/api/recent-jobs").json()["jobs"]

        assert [j["job_id"] for j in jobs] == [done]

    def test_a_job_whose_folder_was_swept_is_not_offered(self, tmp_path):
        gone = "77777777-7777-4777-8777-777777777777"
        app_module.jobs[gone] = _completed(str(tmp_path / gone), 3)

        assert _get("/api/recent-jobs").json()["jobs"] == []

    def test_cloud_answers_404(self, monkeypatch, tmp_path):
        job = "88888888-8888-4888-8888-888888888888"
        app_module.jobs[job] = _completed(_job_dir(tmp_path, job, "Mine", time.time()), 1)
        monkeypatch.setattr(app_module, "BILLING_ENABLED", True)

        assert _get("/api/recent-jobs").status_code == 404
