/* ============================================================
   Job Tracker — Frontend JavaScript
   ============================================================ */

// ---- Sidebar toggle ----
document.addEventListener('DOMContentLoaded', () => {
  const sidebar = document.getElementById('sidebar');
  const toggleBtn = document.getElementById('sidebarToggle');

  if (toggleBtn && sidebar) {
    toggleBtn.addEventListener('click', () => {
      if (window.innerWidth <= 768) {
        sidebar.classList.toggle('mobile-open');
      } else {
        sidebar.classList.toggle('collapsed');
      }
    });
  }

  // ---- Live clock (full date + time) ----
  const clockEl = document.getElementById('clock');
  if (clockEl) {
    const updateClock = () => {
      clockEl.textContent = new Date().toLocaleString('en-US', {
        month: 'short', day: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false,
      });
    };
    updateClock();
    setInterval(updateClock, 1000);
  }

  // ---- Auto-dismiss flash messages ----
  document.querySelectorAll('.alert.alert-success').forEach(el => {
    setTimeout(() => el.classList.remove('show'), 4000);
  });

  // ---- Bootstrap tooltips ----
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    new bootstrap.Tooltip(el);
  });

  // ---- Confirm destructive actions ----
  document.querySelectorAll('[data-confirm]').forEach(el => {
    el.addEventListener('click', e => {
      if (!confirm(el.dataset.confirm)) e.preventDefault();
    });
  });

  // ---- Poll notification count every 60s ----
  pollNotifications();
  setInterval(pollNotifications, 60_000);
});

// ---- Poll unread notification count ----
async function pollNotifications() {
  try {
    const resp = await fetch('/api/notifications/count');
    if (!resp.ok) return;
    const data = await resp.json();
    document.querySelectorAll('.notif-badge').forEach(el => {
      el.textContent = data.unread > 0 ? data.unread : '';
      el.style.display = data.unread > 0 ? '' : 'none';
    });
  } catch (_) { /* silently ignore */ }
}

// ---- Scan status poller (used on dashboard) ----
async function pollScanStatus(intervalMs = 5000) {
  const statusEl = document.getElementById('scanStatus');
  if (!statusEl) return;

  const check = async () => {
    try {
      const resp = await fetch('/api/scan-status');
      const data = await resp.json();
      if (data.status === 'success' || data.status === 'error') {
        location.reload();
      }
    } catch (_) { /* ignore */ }
  };

  const id = setInterval(check, intervalMs);
  // Stop polling after 3 minutes
  setTimeout(() => clearInterval(id), 180_000);
}

// ---- Tooltip toggle ----
(function () {
  const KEY = 'jt_tooltips';
  const btn = document.getElementById('tooltipToggleBtn');

  // Restore saved preference on every page load
  if (localStorage.getItem(KEY) === 'off') {
    document.body.classList.add('tooltips-off');
    if (btn) {
      btn.classList.replace('btn-outline-info', 'btn-outline-secondary');
      btn.title = 'Hints off — click to enable';
    }
  }

  if (btn) {
    btn.addEventListener('click', () => {
      const isOff = document.body.classList.toggle('tooltips-off');
      localStorage.setItem(KEY, isOff ? 'off' : 'on');
      btn.classList.toggle('btn-outline-info', !isOff);
      btn.classList.toggle('btn-outline-secondary', isOff);
      btn.title = isOff ? 'Hints off — click to enable' : 'Hints on — click to disable';
    });
  }
}());

// ---- Score range display (settings page) ----
const scoreRange = document.querySelector('input[name="min_match_score"]');
const scoreVal   = document.getElementById('scoreVal');
if (scoreRange && scoreVal) {
  scoreRange.addEventListener('input', () => {
    scoreVal.textContent = scoreRange.value;
  });
}
