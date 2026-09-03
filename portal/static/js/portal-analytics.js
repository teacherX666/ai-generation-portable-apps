// Lightweight portal analytics: queue events locally and flush them to the backend.
// No third-party SDK, no build step; works the same on Windows and macOS.
(function () {
  const STORAGE_KEY = 'portal_analytics_queue';
  const MAX_QUEUE = 200;

  function readQueue() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); } catch (e) { return []; }
  }
  function writeQueue(queue) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(queue.slice(-MAX_QUEUE))); } catch (e) {}
  }

  async function flush() {
    const queue = readQueue();
    if (!queue.length) return;
    writeQueue([]);
    const failed = [];
    for (const entry of queue) {
      try {
        const res = await fetch('/api/analytics/event', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Workspace-Id': window.workspaceId ? window.workspaceId() : '',
          },
          body: JSON.stringify(entry),
        });
        if (!res.ok) failed.push(entry);
      } catch (e) {
        failed.push(entry);
      }
    }
    if (failed.length) writeQueue(failed);
  }

  window.portalAnalytics = {
    track(event, payload) {
      const entry = {
        event: String(event || ''),
        payload: payload || {},
        ts: Math.floor(Date.now() / 1000),
      };
      const queue = readQueue();
      queue.push(entry);
      writeQueue(queue);
      flush();
    },
    flush,
  };

  flush();
})();