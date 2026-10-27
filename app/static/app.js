// Hundjäger Media Hub - Frontend Application Logic

let currentInspectedData = null;
let selectedFormatId = '720p';
let selectedResolutionLabel = '720p';
let selectedFileSizeStr = '';

const urlInput = document.getElementById('url-input');
const inspectForm = document.getElementById('inspect-form');
const btnInspect = document.getElementById('btn-inspect');
const btnPaste = document.getElementById('btn-paste');
const btnOpenFolder = document.getElementById('btn-open-folder');

const previewCard = document.getElementById('preview-card');
const previewTitle = document.getElementById('preview-title');
const previewSite = document.getElementById('preview-site');
const previewDuration = document.getElementById('preview-duration');
const previewResolutionsCount = document.getElementById('preview-resolutions-count');
const previewThumb = document.getElementById('preview-thumb');
const previewThumbDuration = document.getElementById('preview-thumb-duration');
const resolutionsGrid = document.getElementById('resolutions-grid');
const btnStartDownload = document.getElementById('btn-start-download');

const tasksContainer = document.getElementById('tasks-container');
const emptyTasksState = document.getElementById('empty-tasks-state');
const queueCountBadge = document.getElementById('queue-count-badge');
const btnRetryFailed = document.getElementById('btn-retry-failed');
const btnClearFinished = document.getElementById('btn-clear-finished');
const btnClearAll = document.getElementById('btn-clear-all');
const toastContainer = document.getElementById('toast-container');

// Clipboard Paste
btnPaste.addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      urlInput.value = text.trim();
      urlInput.focus();
      showToast('Pasted link from clipboard', 'info');
    }
  } catch (err) {
    urlInput.focus();
  }
});

// Open Downloads Folder
btnOpenFolder.addEventListener('click', async () => {
  try {
    await fetch('/api/open-downloads', { 
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    showToast('Opened downloads folder', 'success');
  } catch (e) {
    showToast('Failed to open downloads folder', 'error');
  }
});

// Handle Inspect Submit
inspectForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  let url = urlInput.value.trim();
  if (!url) return;

  // Auto-clean if multiple URLs or extra text was pasted
  const urlMatch = url.match(/https?:\/\/[^\s|]+/);
  if (urlMatch) {
    url = urlMatch[0];
    urlInput.value = url;
  }

  setInspectLoading(true);

  try {
    const res = await fetch('/api/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });

    const data = await res.json();
    if (!data.success) {
      showToast(data.error || 'Failed to inspect link', 'error');
      setInspectLoading(false);
      return;
    }

    currentInspectedData = data;
    renderPreview(data);
    showToast('Stream metadata extracted!', 'success');
  } catch (err) {
    showToast('Connection error to server', 'error');
  } finally {
    setInspectLoading(false);
  }
});

function setInspectLoading(loading) {
  const btnText = btnInspect.querySelector('.btn-text');
  const btnSpinner = btnInspect.querySelector('.btn-spinner');
  const btnArrow = btnInspect.querySelector('.btn-arrow');

  if (loading) {
    btnInspect.disabled = true;
    btnText.textContent = 'Inspecting...';
    btnSpinner.classList.remove('hidden');
    btnArrow.classList.add('hidden');
  } else {
    btnInspect.disabled = false;
    btnText.textContent = 'Inspect Stream';
    btnSpinner.classList.add('hidden');
    btnArrow.classList.remove('hidden');
  }
}

// Render Preview Card
function renderPreview(data) {
  previewTitle.textContent = data.title;
  previewSite.textContent = data.extractor || 'Web Stream';
  previewDuration.textContent = data.duration;
  previewThumbDuration.textContent = data.duration;
  previewThumb.src = data.thumbnail || '';

  const resList = data.resolutions || [];
  previewResolutionsCount.textContent = `${resList.length} qualities detected`;

  resolutionsGrid.innerHTML = '';

  function updateDownloadButton(label, sizeStr) {
    const btnText = document.getElementById('btn-download-text');
    if (!btnText) return;
    if (sizeStr && sizeStr !== 'Unknown size' && sizeStr !== 'Smallest Size') {
      btnText.textContent = `Download ${label} (${sizeStr})`;
    } else if (label) {
      btnText.textContent = `Download ${label}`;
    } else {
      btnText.textContent = 'Start Download';
    }
  }

  // Select default resolution: prioritize 720p, else highest available
  const has720 = resList.find(r => r.label === '720p');
  const defaultSelection = has720 ? has720 : (resList[0] || { label: 'Best', format_id: 'best', filesize_str: '' });
  selectedFormatId = defaultSelection.format_id;
  selectedResolutionLabel = defaultSelection.label;
  selectedFileSizeStr = defaultSelection.filesize_str || '';
  updateDownloadButton(selectedResolutionLabel, selectedFileSizeStr);

  resList.forEach(item => {
    const card = document.createElement('div');
    card.className = `res-card ${item.label === selectedResolutionLabel ? 'selected' : ''}`;
    
    const isRecommended = item.label === '720p';
    const isFullHD = item.label === '1080p';

    card.innerHTML = `
      <div class="res-top">
        <span class="res-badge">${item.label}</span>
        ${isRecommended ? '<span class="res-rec-tag">Recommended</span>' : ''}
        ${isFullHD ? '<span class="res-rec-tag" style="background:rgba(139,92,246,0.15);color:#c4b5fd;border-color:rgba(139,92,246,0.3)">Full HD</span>' : ''}
      </div>
      <div class="res-bottom">
        <span class="res-size">${item.filesize_str || 'Estimated stream'}</span>
        <span class="res-ext">${(item.ext || 'mp4').toUpperCase()}</span>
      </div>
    `;

    card.addEventListener('click', () => {
      document.querySelectorAll('.res-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      selectedFormatId = item.format_id;
      selectedResolutionLabel = item.label;
      selectedFileSizeStr = item.filesize_str || '';
      updateDownloadButton(selectedResolutionLabel, selectedFileSizeStr);
    });

    resolutionsGrid.appendChild(card);
  });

  // Also append an "Audio Only" option if not present
  const audioSizeStr = data.audio_size || 'Smallest Size';
  const audioCard = document.createElement('div');
  audioCard.className = `res-card ${selectedResolutionLabel === 'Audio Only' ? 'selected' : ''}`;
  audioCard.innerHTML = `
    <div class="res-top">
      <span class="res-badge">Audio</span>
      <span class="res-rec-tag" style="background:rgba(6,182,212,0.15);color:#38bdf8;border-color:rgba(6,182,212,0.3)">MP3/M4A</span>
    </div>
    <div class="res-bottom">
      <span class="res-size">${audioSizeStr}</span>
      <span class="res-ext">AUDIO</span>
    </div>
  `;
  audioCard.addEventListener('click', () => {
    document.querySelectorAll('.res-card').forEach(c => c.classList.remove('selected'));
    audioCard.classList.add('selected');
    selectedFormatId = 'bestaudio/best';
    selectedResolutionLabel = 'Audio Only';
    selectedFileSizeStr = audioSizeStr;
    updateDownloadButton(selectedResolutionLabel, selectedFileSizeStr);
  });
  resolutionsGrid.appendChild(audioCard);

  previewCard.classList.remove('hidden');
  previewCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Start Download
btnStartDownload.addEventListener('click', async () => {
  if (!currentInspectedData) return;

  btnStartDownload.disabled = true;

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: currentInspectedData.url,
        format_id: selectedFormatId,
        resolution: selectedResolutionLabel,
        title: currentInspectedData.title,
        filesize_str: selectedFileSizeStr
      })
    });

    const data = await res.json();
    if (data.success) {
      showToast(`Started download: ${selectedResolutionLabel}`, 'success');
      pollTasks(); // Immediate refresh
    } else {
      showToast(data.error || 'Failed to start download', 'error');
    }
  } catch (err) {
    showToast('Failed to communicate with download worker', 'error');
  } finally {
    btnStartDownload.disabled = false;
  }
});

// Background Tasks Polling
async function pollTasks() {
  try {
    const res = await fetch('/api/tasks');
    const data = await res.json();
    if (data.success && data.tasks) {
      renderTasks(data.tasks);
    }
  } catch (e) {
    // Ignore transient network errors
  }
}

function renderTasks(tasks) {
  if (!tasks || tasks.length === 0) {
    emptyTasksState.classList.remove('hidden');
    queueCountBadge.textContent = '0 active';
    if (btnRetryFailed) btnRetryFailed.disabled = true;
    if (btnClearFinished) btnClearFinished.disabled = true;
    if (btnClearAll) btnClearAll.disabled = true;
    tasksContainer.querySelectorAll('.task-item').forEach(el => el.remove());
    return;
  }

  emptyTasksState.classList.add('hidden');
  const activeCount = tasks.filter(t => t.status === 'downloading' || t.status === 'starting').length;
  queueCountBadge.textContent = `${activeCount} active`;

  const hasFailed = tasks.some(t => ['error', 'cancelled'].includes(t.status));
  if (btnRetryFailed) btnRetryFailed.disabled = !hasFailed;

  const hasClearable = tasks.some(t => ['finished', 'cancelled', 'error'].includes(t.status));
  if (btnClearFinished) btnClearFinished.disabled = !hasClearable;
  if (btnClearAll) btnClearAll.disabled = false;

  // Sort descending by created_at
  const sorted = [...tasks].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

  tasksContainer.querySelectorAll('.task-item').forEach(el => el.remove());

  sorted.forEach(t => {
    const item = document.createElement('div');
    item.className = `task-item status-${t.status}`;
    item.id = `task-${t.id}`;

    const percent = Math.min(100, Math.max(0, t.percent || 0));
    const speed = (t.speed && !['--', 'Unknown', 'N/A', 'NA'].includes(t.speed.trim())) ? t.speed.trim() : '';
    const eta = (t.eta && !['--', 'Unknown', 'N/A', 'NA', '00:00'].includes(t.eta.trim())) ? `ETA: ${t.eta.trim()}` : '';
    function isValidSize(val) {
      if (!val) return false;
      const s = String(val).trim().toUpperCase();
      return s !== '' && s !== '--' && s !== 'N/A' && s !== 'NA' && s !== 'UNKNOWN' && s !== 'NONE';
    }

    const downloaded = isValidSize(t.downloaded) ? t.downloaded.trim() : '';
    const total = isValidSize(t.total) ? t.total.trim() : (isValidSize(t.filesize_str) ? t.filesize_str.trim() : '');

    let mbMeter = '';
    if (t.status === 'finished') {
      const finalSize = total || downloaded;
      if (finalSize) {
        mbMeter = finalSize.replace(/^~/, '');
      }
    } else if (downloaded && total && downloaded !== total) {
      mbMeter = `${downloaded} / ${total}`;
    } else if (downloaded) {
      mbMeter = downloaded;
    } else if (total) {
      mbMeter = total;
    }

    let statusText = '';
    if (t.status === 'starting') statusText = 'Starting stream...';
    else if (t.status === 'downloading') statusText = `${percent.toFixed(1)}%`;
    else if (t.status === 'finished') statusText = '100% Complete';
    else if (t.status === 'cancelled') statusText = 'Cancelled';
    else if (t.status === 'error') statusText = `Error: ${t.error || 'Failed'}`;

    item.innerHTML = `
      <div class="task-top">
        <div class="task-title-group">
          <h4 class="task-title" title="${t.title}">${t.title}</h4>
          <span class="task-res-badge">${t.resolution || 'Auto'}</span>
        </div>
        <button class="btn-delete-task" title="Remove from list" onclick="deleteTask('${t.id}')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <div class="progress-bar-track">
        <div class="progress-bar-fill" style="width: ${percent}%"></div>
      </div>

      <div class="task-meta">
        <div class="task-stats">
          <span class="task-percent">${statusText}</span>
          ${mbMeter ? `<span class="task-mb-meter">${mbMeter}</span>` : ''}
          ${speed ? `<span class="task-speed">${speed}</span>` : ''}
          ${eta ? `<span class="task-eta">${eta}</span>` : ''}
        </div>

        <div class="task-actions">
          ${t.status === 'downloading' || t.status === 'starting' ? `
            <button class="btn-mini btn-mini-danger" onclick="cancelTask('${t.id}')">Cancel</button>
          ` : ''}
          ${t.status === 'error' || t.status === 'cancelled' ? `
            <button class="btn-mini btn-mini-accent" onclick="retryTask('${t.id}')" title="Resume and finish downloading">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                <path d="M23 4v6h-6"></path>
                <path d="M1 20v-6h6"></path>
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
              </svg>
              <span>Resume</span>
            </button>
          ` : ''}
          ${t.status === 'finished' ? `
            <button class="btn-mini" onclick="openFolder()">Show in Finder</button>
          ` : ''}
        </div>
      </div>
    `;

    tasksContainer.appendChild(item);
  });
}

window.cancelTask = async function(taskId) {
  try {
    await fetch('/api/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId })
    });
    showToast('Download cancelled', 'info');
    pollTasks();
  } catch (e) {
    showToast('Failed to cancel task', 'error');
  }
};

window.deleteTask = async function(taskId) {
  try {
    await fetch('/api/tasks/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId })
    });
    showToast('Task removed from queue', 'info');
    pollTasks();
  } catch (e) {
    showToast('Failed to remove task', 'error');
  }
};

window.clearTasks = async function(mode = 'finished') {
  try {
    await fetch('/api/tasks/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode })
    });
    showToast(mode === 'all' ? 'Cleared all downloads' : 'Cleared finished downloads', 'success');
    pollTasks();
  } catch (e) {
    showToast('Failed to clear queue', 'error');
  }
};

window.retryTask = async function(taskId) {
  try {
    const res = await fetch('/api/tasks/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Resuming download from where it stopped...', 'info');
      pollTasks();
    } else {
      showToast(data.error || 'Failed to resume download', 'error');
    }
  } catch (e) {
    showToast('Failed to resume download', 'error');
  }
};

window.retryAllFailed = async function() {
  try {
    const res = await fetch('/api/tasks/retry-all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Resuming ${data.count || ''} failed downloads...`, 'info');
      pollTasks();
    } else {
      showToast('Failed to resume downloads', 'error');
    }
  } catch (e) {
    showToast('Failed to resume downloads', 'error');
  }
};

if (btnRetryFailed) {
  btnRetryFailed.addEventListener('click', () => retryAllFailed());
}

if (btnClearFinished) {
  btnClearFinished.addEventListener('click', () => clearTasks('finished'));
}

if (btnClearAll) {
  btnClearAll.addEventListener('click', () => {
    if (confirm('Cancel any active downloads and clear the entire queue?')) {
      clearTasks('all');
    }
  });
}

window.openFolder = async function() {
  await fetch('/api/open-downloads', { 
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  });
};

// Toast notification system
function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;

  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// Initial status check and periodic task polling
async function init() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (data.status === 'online') {
      const statusLabel = document.querySelector('.status-label');
      if (statusLabel) statusLabel.textContent = 'yt-dlp Engine: Active';
    }
  } catch (e) {
    const statusLabel = document.querySelector('.status-label');
    if (statusLabel) statusLabel.textContent = 'Offline / Connecting';
  }

  pollTasks();
  setInterval(pollTasks, 1200);
}

init();
