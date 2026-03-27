// Ridge Detector v2 - PWA Application
'use strict';

const App = {
  ws: null,
  wsConnected: false,
  currentMode: 'IDLE',
  currentPage: 'dashboard',
  // Annotation state
  annSession: null,
  annFrame: null,
  annPoints: [],
  annImage: null,
  annCanvas: null,
  annCtx: null,

  // ----------------------------------------------------------------
  // Init
  // ----------------------------------------------------------------
  init() {
    this.setupRouter();
    this.connectWebSocket();
    this.setupAnnotationCanvas();

    // Initial route
    this.handleRoute();

    // Periodic status fetch as fallback
    setInterval(() => this.fetchStatus(), 3000);
  },

  // ----------------------------------------------------------------
  // Router
  // ----------------------------------------------------------------
  setupRouter() {
    window.addEventListener('hashchange', () => this.handleRoute());
    document.querySelectorAll('nav a').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        window.location.hash = a.getAttribute('href');
      });
    });
  },

  handleRoute() {
    const hash = window.location.hash || '#/';
    const parts = hash.slice(2).split('/'); // Remove '#/'

    // Update nav
    document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));

    if (parts[0] === '' || parts[0] === undefined) {
      this.showPage('dashboard');
    } else if (parts[0] === 'sessions' && parts[1]) {
      this.showPage('annotation');
      this.loadAnnotationView(parts[1]);
    } else if (parts[0] === 'sessions') {
      this.showPage('sessions');
      this.loadSessions();
    } else if (parts[0] === 'training') {
      this.showPage('training');
      this.loadTrainingInfo();
    } else {
      this.showPage('dashboard');
    }
  },

  showPage(name) {
    this.currentPage = name;
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));

    const pageMap = {
      dashboard: 'page-dashboard',
      sessions: 'page-sessions',
      annotation: 'page-annotation',
      training: 'page-training',
    };

    const el = document.getElementById(pageMap[name]);
    if (el) el.classList.add('active');

    // Highlight nav
    const navMap = {
      dashboard: '[data-page="dashboard"]',
      sessions: '[data-page="sessions"]',
      annotation: '[data-page="sessions"]',
      training: '[data-page="training"]',
    };
    const navEl = document.querySelector(navMap[name]);
    if (navEl) navEl.classList.add('active');
  },

  navigate(page) {
    if (page === 'sessions') window.location.hash = '#/sessions';
    else if (page === 'dashboard') window.location.hash = '#/';
    else if (page === 'training') window.location.hash = '#/training';
  },

  // ----------------------------------------------------------------
  // WebSocket
  // ----------------------------------------------------------------
  connectWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws`;

    try {
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        this.wsConnected = true;
        this.updateConnectionDot(true);
        this.ws.send(JSON.stringify({
          type: 'subscribe',
          channels: ['status', 'detection', 'training', 'log', 'frame']
        }));
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          this.handleWsMessage(msg);
        } catch (e) { /* ignore parse errors */ }
      };

      this.ws.onclose = () => {
        this.wsConnected = false;
        this.updateConnectionDot(false);
        setTimeout(() => this.connectWebSocket(), 2000);
      };

      this.ws.onerror = () => {
        this.ws.close();
      };
    } catch (e) {
      setTimeout(() => this.connectWebSocket(), 2000);
    }
  },

  handleWsMessage(msg) {
    switch (msg.type) {
      case 'status':
        this.updateStatus(msg.data);
        break;
      case 'detection':
        this.updateDetection(msg.data);
        break;
      case 'training':
        this.updateTraining(msg.data);
        break;
      case 'log':
        this.appendLogs(msg.data);
        break;
      case 'frame':
        this.updateFrame(msg.data);
        break;
    }
  },

  updateConnectionDot(connected) {
    const dot = document.getElementById('ws-dot');
    if (dot) {
      dot.className = connected
        ? 'connection-dot connected'
        : 'connection-dot disconnected';
    }
  },

  // ----------------------------------------------------------------
  // Status updates
  // ----------------------------------------------------------------
  async fetchStatus() {
    try {
      const res = await fetch('/api/status');
      if (res.ok) {
        const data = await res.json();
        this.updateStatus(data);
      }
    } catch (e) { /* offline */ }
  },

  updateStatus(data) {
    if (!data) return;
    const mode = data.mode || 'IDLE';
    this.currentMode = mode;

    // Mode badge
    const badge = document.getElementById('mode-badge');
    if (badge) {
      badge.textContent = mode;
      badge.className = `mode-badge mode-${mode}`;
    }

    // Control buttons
    const isIdle = mode === 'IDLE';
    const btnRec = document.getElementById('btn-record');
    const btnDet = document.getElementById('btn-detect');
    const btnStop = document.getElementById('btn-stop');
    if (btnRec) btnRec.disabled = !isIdle;
    if (btnDet) btnDet.disabled = !isIdle;
    if (btnStop) btnStop.disabled = isIdle;

    // Detection data if present
    if (data.detection) {
      this.updateDetection(data.detection);
    }

    // Training data if present
    if (data.training) {
      this.updateTraining(data.training);
    }
  },

  updateDetection(data) {
    if (!data) return;
    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setVal('stat-a', data.a != null ? Number(data.a).toFixed(4) : '--');
    setVal('stat-b', data.b != null ? Number(data.b).toFixed(1) : '--');
    setVal('stat-fps', data.fps != null ? Number(data.fps).toFixed(1) : '--');
    setVal('stat-serial', data.serial_count != null
      ? `${data.serial_status || ''} ${data.serial_count}` : '--');
  },

  updateTraining(data) {
    if (!data) return;
    const phase = document.getElementById('training-phase');
    const progress = document.getElementById('training-progress');
    const detail = document.getElementById('training-detail');
    const btnStart = document.getElementById('btn-train-start');
    const btnStop = document.getElementById('btn-train-stop');

    if (data.running) {
      const pct = data.total_epochs > 0
        ? Math.round((data.epoch / data.total_epochs) * 100) : 0;
      if (phase) phase.textContent = `Training: ${data.phase || 'running'}`;
      if (progress) progress.style.width = `${pct}%`;
      if (detail) detail.textContent =
        `Epoch ${data.epoch}/${data.total_epochs} | Loss: ${Number(data.loss || 0).toFixed(4)}`;
      if (btnStart) btnStart.disabled = true;
      if (btnStop) btnStop.disabled = false;
    } else {
      if (phase) phase.textContent = data.phase || 'Not running';
      if (progress) progress.style.width = '0%';
      if (detail) detail.textContent = '';
      if (btnStart) btnStart.disabled = false;
      if (btnStop) btnStop.disabled = true;
    }
  },

  updateFrame(base64Data) {
    if (this.currentPage !== 'dashboard') return;
    const img = document.getElementById('live-preview');
    if (img && base64Data) {
      img.src = `data:image/jpeg;base64,${base64Data}`;
    }
  },

  appendLogs(logs) {
    const area = document.getElementById('log-area');
    if (!area || !logs) return;
    for (const line of logs) {
      area.textContent += line + '\n';
    }
    area.scrollTop = area.scrollHeight;
  },

  // ----------------------------------------------------------------
  // Mode control
  // ----------------------------------------------------------------
  async setMode(mode) {
    try {
      const res = await fetch('/api/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  // ----------------------------------------------------------------
  // Sessions
  // ----------------------------------------------------------------
  async loadSessions() {
    const container = document.getElementById('session-list');
    if (!container) return;
    container.innerHTML = '<p style="color:var(--text2)">Loading...</p>';

    try {
      const res = await fetch('/api/sessions');
      const sessions = await res.json();

      if (sessions.length === 0) {
        container.innerHTML = '<div class="empty-state">No recording sessions yet</div>';
        return;
      }

      container.innerHTML = sessions.map(s => `
        <div class="session-item" onclick="window.location.hash='#/sessions/${s.name}'">
          <div class="session-info">
            <h4>${s.name}</h4>
            <span>${s.frame_count} frames | ${s.annotated_count} annotated</span>
          </div>
          <div class="session-meta">
            ${s.svo2_size_mb} MB
            <br>
            <button class="btn btn-danger" style="font-size:11px; padding:4px 8px; margin-top:4px;"
              onclick="event.stopPropagation(); App.deleteSession('${s.name}')">Delete</button>
          </div>
        </div>
      `).join('');
    } catch (e) {
      container.innerHTML = '<div class="empty-state">Failed to load sessions</div>';
    }

    // Also update training page stats
    this.updateDatasetStats();
  },

  async deleteSession(name) {
    if (!confirm(`Delete session ${name}? This cannot be undone.`)) return;
    try {
      await fetch(`/api/sessions/${name}`, { method: 'DELETE' });
      this.loadSessions();
    } catch (e) {
      alert('Failed to delete session');
    }
  },

  // ----------------------------------------------------------------
  // Annotation view
  // ----------------------------------------------------------------
  async loadAnnotationView(sessionName) {
    this.annSession = sessionName;
    this.annFrame = null;
    this.annPoints = [];

    document.getElementById('ann-session-name').textContent = sessionName;
    document.getElementById('ann-editor').style.display = 'none';

    const grid = document.getElementById('frame-grid');
    grid.innerHTML = '<p style="color:var(--text2)">Loading...</p>';

    try {
      const res = await fetch(`/api/sessions/${sessionName}/frames`);
      const frames = await res.json();

      if (frames.length === 0) {
        grid.innerHTML = '<div class="empty-state">No frames in this session</div>';
        return;
      }

      grid.innerHTML = frames.map(f => `
        <div class="frame-thumb ${f.annotated ? 'annotated' : ''}"
             id="thumb-${f.filename}"
             onclick="App.selectFrame('${f.filename}')">
          <img src="/api/sessions/${sessionName}/frames/${f.filename}"
               loading="lazy" alt="${f.filename}">
          ${f.annotated ? '<span class="badge-dot"></span>' : ''}
        </div>
      `).join('');
    } catch (e) {
      grid.innerHTML = '<div class="empty-state">Failed to load frames</div>';
    }
  },

  async selectFrame(filename) {
    this.annFrame = filename;
    this.annPoints = [];

    // Highlight selection
    document.querySelectorAll('.frame-thumb').forEach(el => el.classList.remove('selected'));
    const thumb = document.getElementById(`thumb-${filename}`);
    if (thumb) thumb.classList.add('selected');

    document.getElementById('ann-frame-name').textContent = filename;
    document.getElementById('ann-editor').style.display = 'block';

    // Load image into canvas
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      this.annImage = img;
      this.resizeCanvas();
      this.drawAnnotation();
    };
    img.src = `/api/sessions/${this.annSession}/frames/${filename}`;

    // Load existing annotation
    try {
      const res = await fetch(
        `/api/sessions/${this.annSession}/frames/${filename}/annotation`
      );
      const data = await res.json();
      if (data.exists && data.points && data.points.length === 4) {
        this.annPoints = data.points;
      }
    } catch (e) { /* no annotation */ }
  },

  // ----------------------------------------------------------------
  // Annotation Canvas
  // ----------------------------------------------------------------
  setupAnnotationCanvas() {
    const canvas = document.getElementById('annotation-canvas');
    if (!canvas) return;
    this.annCanvas = canvas;
    this.annCtx = canvas.getContext('2d');

    // Touch/click handler
    const handler = (e) => {
      e.preventDefault();
      if (!this.annImage) return;
      if (this.annPoints.length >= 4) return;

      const rect = canvas.getBoundingClientRect();
      let clientX, clientY;
      if (e.touches) {
        clientX = e.touches[0].clientX;
        clientY = e.touches[0].clientY;
      } else {
        clientX = e.clientX;
        clientY = e.clientY;
      }

      // Normalized coordinates (0-1)
      const x = (clientX - rect.left) / rect.width;
      const y = (clientY - rect.top) / rect.height;

      this.annPoints.push([
        Math.max(0, Math.min(1, x)),
        Math.max(0, Math.min(1, y)),
      ]);

      this.drawAnnotation();
    };

    canvas.addEventListener('click', handler);
    canvas.addEventListener('touchstart', handler, { passive: false });

    window.addEventListener('resize', () => {
      if (this.annImage) {
        this.resizeCanvas();
        this.drawAnnotation();
      }
    });
  },

  resizeCanvas() {
    if (!this.annCanvas || !this.annImage) return;
    const container = document.getElementById('annotation-area');
    const w = container.clientWidth;
    const aspect = this.annImage.naturalHeight / this.annImage.naturalWidth;
    const h = Math.round(w * aspect);
    this.annCanvas.width = w;
    this.annCanvas.height = h;
  },

  drawAnnotation() {
    const ctx = this.annCtx;
    const canvas = this.annCanvas;
    if (!ctx || !canvas || !this.annImage) return;

    const w = canvas.width;
    const h = canvas.height;

    // Draw image
    ctx.drawImage(this.annImage, 0, 0, w, h);

    if (this.annPoints.length === 0) return;

    // Draw polygon
    ctx.strokeStyle = '#e94560';
    ctx.lineWidth = 2;
    ctx.fillStyle = 'rgba(233, 69, 96, 0.2)';

    ctx.beginPath();
    for (let i = 0; i < this.annPoints.length; i++) {
      const px = this.annPoints[i][0] * w;
      const py = this.annPoints[i][1] * h;
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    if (this.annPoints.length === 4) {
      ctx.closePath();
      ctx.fill();
    }
    ctx.stroke();

    // Draw points
    for (let i = 0; i < this.annPoints.length; i++) {
      const px = this.annPoints[i][0] * w;
      const py = this.annPoints[i][1] * h;
      ctx.fillStyle = '#e94560';
      ctx.beginPath();
      ctx.arc(px, py, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(String(i + 1), px, py + 3);
    }
  },

  async saveAnnotation() {
    if (!this.annSession || !this.annFrame) return;
    if (this.annPoints.length !== 4) {
      alert('Place exactly 4 points before saving.');
      return;
    }

    try {
      const res = await fetch(
        `/api/sessions/${this.annSession}/frames/${this.annFrame}/annotation`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ points: this.annPoints }),
        }
      );
      if (res.ok) {
        // Update thumbnail badge
        const thumb = document.getElementById(`thumb-${this.annFrame}`);
        if (thumb && !thumb.classList.contains('annotated')) {
          thumb.classList.add('annotated');
          const dot = document.createElement('span');
          dot.className = 'badge-dot';
          thumb.appendChild(dot);
        }
      } else {
        const data = await res.json();
        alert(data.detail || 'Failed to save');
      }
    } catch (e) {
      alert('Connection error');
    }
  },

  clearAnnotation() {
    this.annPoints = [];
    this.drawAnnotation();
  },

  async deleteAnnotation() {
    if (!this.annSession || !this.annFrame) return;
    try {
      await fetch(
        `/api/sessions/${this.annSession}/frames/${this.annFrame}/annotation`,
        { method: 'DELETE' }
      );
      this.annPoints = [];
      this.drawAnnotation();

      // Update thumbnail
      const thumb = document.getElementById(`thumb-${this.annFrame}`);
      if (thumb) {
        thumb.classList.remove('annotated');
        const dot = thumb.querySelector('.badge-dot');
        if (dot) dot.remove();
      }
    } catch (e) {
      alert('Failed to delete annotation');
    }
  },

  // ----------------------------------------------------------------
  // Training
  // ----------------------------------------------------------------
  // Cached session data for stats recalculation
  _trainSessions: [],

  async loadTrainingInfo() {
    // Fetch sessions once and share the result for both stats and session list
    this._trainSessions = [];
    try {
      const res = await fetch('/api/sessions');
      this._trainSessions = await res.json();
    } catch (e) { /* offline */ }

    this.renderTrainSessionList(this._trainSessions);
    this.recalcSelectedStats();

    // Fetch current training status
    try {
      const res = await fetch('/api/training/status');
      if (res.ok) {
        const data = await res.json();
        this.updateTraining(data);
      }
    } catch (e) { /* offline */ }
  },

  renderTrainSessionList(sessions) {
    const container = document.getElementById('train-session-list');
    if (!container) return;

    // Only show sessions that have annotated frames
    const annotated = (sessions || []).filter(s => s.annotated_count > 0);
    if (annotated.length === 0) {
      container.innerHTML = '<div class="empty-state">No annotated sessions</div>';
      return;
    }

    container.innerHTML = annotated.map(s => `
      <label class="train-session-row">
        <input type="checkbox" class="train-session-cb" value="${s.name}" checked
               onchange="App.recalcSelectedStats()">
        <span class="train-session-name">${s.name}</span>
        <span class="train-session-count">${s.annotated_count} annotated</span>
      </label>
    `).join('');
  },

  toggleAllTrainSessions(checked) {
    document.querySelectorAll('.train-session-cb').forEach(cb => {
      cb.checked = checked;
    });
    this.recalcSelectedStats();
  },

  getSelectedTrainSessions() {
    const cbs = document.querySelectorAll('.train-session-cb:checked');
    return Array.from(cbs).map(cb => cb.value);
  },

  recalcSelectedStats() {
    const selected = new Set(this.getSelectedTrainSessions());
    let totalFrames = 0;
    let totalAnnotated = 0;
    for (const s of (this._trainSessions || [])) {
      if (selected.has(s.name)) {
        totalFrames += s.frame_count || 0;
        totalAnnotated += s.annotated_count || 0;
      }
    }

    const el1 = document.getElementById('stat-total-frames');
    const el2 = document.getElementById('stat-total-annotated');
    if (el1) el1.textContent = totalFrames;
    if (el2) el2.textContent = totalAnnotated;
  },

  async updateDatasetStats() {
    try {
      const res = await fetch('/api/sessions');
      this._trainSessions = await res.json();
      this.recalcSelectedStats();
    } catch (e) { /* offline */ }
  },

  async startTraining() {
    const epochs = parseInt(document.getElementById('train-epochs').value) || 50;
    const batchSize = parseInt(document.getElementById('train-batch').value) || 4;
    const imgSize = parseInt(document.getElementById('train-imgsize').value) || 640;
    const lr0 = parseFloat(document.getElementById('train-lr0').value) || 0.001;
    const lrf = parseFloat(document.getElementById('train-lrf').value) || 0.1;
    const freeze = parseInt(document.getElementById('train-freeze').value) || 10;
    const flipud = parseFloat(document.getElementById('train-flipud').value) || 0.5;
    const amp = document.getElementById('train-amp').value === 'true';
    const sessions = this.getSelectedTrainSessions();

    if (sessions.length === 0) {
      alert('Select at least one session.');
      return;
    }

    try {
      const res = await fetch('/api/training/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          epochs: epochs,
          batch_size: batchSize,
          img_size: imgSize,
          sessions: sessions,
          lr0: lr0,
          lrf: lrf,
          freeze: freeze,
          flipud: flipud,
          amp: amp,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to start training');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  async stopTraining() {
    try {
      const res = await fetch('/api/training/stop', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to stop');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },
};

// Register service worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// Start
document.addEventListener('DOMContentLoaded', () => App.init());
