import React, { useState, useEffect } from 'react';
import { Loader2, FolderOpen } from 'lucide-react';
import { apiJson } from '../lib/api';

// Self-host only: the finished jobs still on this server's disk, so one can be
// reopened after its saved session is gone (a reset, another browser, a day
// later). Cloud users have the History tab instead. Renders nothing while
// loading, on error, or when there is nothing to reopen.
const COLLAPSED_COUNT = 5;

export default function RecentJobs({ onOpenJob }) {
  const [jobs, setJobs] = useState([]);
  const [showAll, setShowAll] = useState(false);
  const [opening, setOpening] = useState(null);
  const [openError, setOpenError] = useState('');

  useEffect(() => {
    let cancelled = false;
    apiJson('/api/recent-jobs')
      .then((d) => { if (!cancelled) setJobs(d.jobs || []); })
      .catch(() => { /* no list, no section: the uploader stays the screen */ });
    return () => { cancelled = true; };
  }, []);

  if (jobs.length === 0) return null;

  const handleOpen = async (jobId) => {
    if (opening) return;
    setOpening(jobId);
    setOpenError('');
    try {
      await onOpenJob(jobId);
    } catch {
      setOpenError('Could not open this project. Its files may have been cleaned up.');
      setOpening(null);
    }
  };

  const fmtDate = (seconds) => new Date(seconds * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const visible = showAll ? jobs : jobs.slice(0, COLLAPSED_COUNT);

  return (
    <div className="w-full text-left">
      <p className="eyebrow mb-2">Recent projects</p>
      <ul className="border border-rule rounded-lg divide-y divide-rule bg-paper2">
        {visible.map((job) => (
          <li key={job.job_id} className="flex items-center gap-3 px-3 py-2.5">
            <div className="min-w-0 flex-1">
              <p className="text-sm text-ink truncate" title={job.title}>{job.title}</p>
              <p className="readout mt-0.5">
                {fmtDate(job.finished_at)} · {job.clip_count} clip{job.clip_count === 1 ? '' : 's'}
              </p>
            </div>
            <button
              onClick={() => handleOpen(job.job_id)}
              disabled={!!opening}
              className="btn-ghost px-3 py-2 text-xs shrink-0 flex items-center gap-1.5"
              title="Open this project's clips to keep editing or publish them"
            >
              {opening === job.job_id ? <Loader2 size={14} className="animate-spin" /> : <FolderOpen size={14} />}
              Open
            </button>
          </li>
        ))}
      </ul>
      {jobs.length > COLLAPSED_COUNT && (
        <button
          onClick={() => setShowAll((v) => !v)}
          className="mt-2 text-xs text-muted hover:text-ink transition-colors"
        >
          {showAll ? 'Show fewer' : `Show all ${jobs.length}`}
        </button>
      )}
      {openError && <p className="mt-2 text-xs text-danger">{openError}</p>}
    </div>
  );
}
