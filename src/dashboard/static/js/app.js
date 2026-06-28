/* ═══════════════════════════════════════════════════════════════
   Dataset Sync — Live Dashboard Frontend Logic
   Socket.IO + Chart.js real-time visualization
   Supports capture cards (Sony A6700 etc.) with device switching
   ═══════════════════════════════════════════════════════════════ */

// ─── Socket.IO Connection ────────────────────────────────────
const socket = io();

// ─── State ───────────────────────────────────────────────────
let isRecording = false;
let recStartTime = null;
let recDuration = 0;
let statusInterval = null;
let timerInterval = null;

// Waveform data buffer (ring buffer of last 200 samples)
const WAVE_MAX = 200;
const waveData = [];
const waveLabels = [];

// CSI amplitude snapshot
let csiAmps = new Array(52).fill(0);

// ─── Chart.js Setup ──────────────────────────────────────────

// Waveform Chart (Disabled to prevent browser freezing under multi-modal load)
/*
const waveCtx = document.getElementById('waveform-canvas').getContext('2d');
const waveChart = new Chart(waveCtx, {
    type: 'line',
    data: {
        labels: waveLabels,
        datasets: [{
            data: waveData,
            borderColor: '#f87171',
            backgroundColor: 'rgba(248, 113, 113, 0.05)',
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.35,
            fill: true,
        }]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        scales: {
            x: { display: false },
            y: {
                display: false,
                min: 0,
                max: 127,
            }
        },
        options: {
            legend: { display: false },
            tooltip: { enabled: false }
        },
        elements: { line: { borderJoinStyle: 'round' } }
    }
});
*/

// CSI Subcarrier Chart
const csiCtx = document.getElementById('csi-canvas').getContext('2d');
const csiLabels = Array.from({ length: 52 }, (_, i) => i);
const csiChart = new Chart(csiCtx, {
    type: 'bar',
    data: {
        labels: csiLabels,
        datasets: [{
            data: csiAmps,
            backgroundColor: (ctx) => {
                const gradient = csiCtx.createLinearGradient(0, 0, ctx.chart.width, 0);
                gradient.addColorStop(0, 'rgba(167, 139, 250, 0.7)');
                gradient.addColorStop(0.5, 'rgba(34, 211, 238, 0.7)');
                gradient.addColorStop(1, 'rgba(78, 140, 255, 0.7)');
                return gradient;
            },
            borderColor: 'transparent',
            borderRadius: 2,
            borderSkipped: false,
        }]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 150 },
        scales: {
            x: {
                display: true,
                grid: { color: 'rgba(100, 120, 200, 0.06)' },
                ticks: {
                    display: true,
                    color: 'rgba(90, 100, 120, 0.5)',
                    font: { size: 8, family: "'JetBrains Mono', monospace" },
                    maxTicksLimit: 10
                },
                title: {
                    display: true,
                    text: 'Subcarrier Index',
                    color: 'rgba(90, 100, 120, 0.5)',
                    font: { size: 9, family: "'Inter', sans-serif" }
                }
            },
            y: {
                display: true,
                grid: { color: 'rgba(100, 120, 200, 0.06)' },
                ticks: {
                    display: true,
                    color: 'rgba(90, 100, 120, 0.5)',
                    font: { size: 8, family: "'JetBrains Mono', monospace" },
                    maxTicksLimit: 5
                },
                title: {
                    display: true,
                    text: 'Amplitude',
                    color: 'rgba(90, 100, 120, 0.5)',
                    font: { size: 9, family: "'Inter', sans-serif" }
                }
            }
        },
        plugins: {
            legend: { display: false },
            tooltip: {
                backgroundColor: 'rgba(14, 18, 30, 0.9)',
                titleColor: '#e8ecf4',
                bodyColor: '#8892a8',
                borderColor: 'rgba(100, 120, 200, 0.2)',
                borderWidth: 1,
                cornerRadius: 6,
                titleFont: { family: "'Inter', sans-serif", weight: 600, size: 11 },
                bodyFont: { family: "'JetBrains Mono', monospace", size: 10 },
                callbacks: {
                    title: (items) => `Subcarrier ${items[0].label}`,
                    label: (item) => `Amplitude: ${item.raw.toFixed(1)}`
                }
            }
        }
    }
});

// EMG Sparkline Chart
const emgCtx = document.getElementById('emg-canvas').getContext('2d');
const emgLabels = Array.from({ length: 16 }, (_, i) => `CH${i+1}`);
let emgData = new Array(16).fill(0);
const emgChart = new Chart(emgCtx, {
    type: 'bar',
    data: {
        labels: emgLabels,
        datasets: [{
            data: emgData,
            backgroundColor: 'rgba(52, 211, 153, 0.7)',
            borderColor: 'transparent',
            borderRadius: 2
        }]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        scales: {
            x: {
                grid: { display: false },
                ticks: { color: 'rgba(90, 100, 120, 0.6)', font: { size: 9 } }
            },
            y: {
                grid: { color: 'rgba(100, 120, 200, 0.06)' },
                ticks: { color: 'rgba(90, 100, 120, 0.5)' },
                min: 0,
                max: 4095
            }
        },
        plugins: { legend: { display: false }, tooltip: { enabled: false } }
    }
});

// ─── Gauge Helpers ───────────────────────────────────────────

const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 52; // ~326.7

function setGauge(arcId, value, max) {
    const el = document.getElementById(arcId);
    if (!el) return;
    const pct = Math.min(value / max, 1);
    const offset = GAUGE_CIRCUMFERENCE * (1 - pct);
    el.style.strokeDashoffset = offset;
}

function animateValue(elId, newVal) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = newVal;
    el.style.transform = 'scale(1.08)';
    setTimeout(() => { el.style.transform = 'scale(1)'; }, 150);
}

// ─── Camera Info ─────────────────────────────────────────────

function updateCameraInfo(info) {
    if (!info) return;
    const resEl = document.getElementById('cam-resolution');
    const fpsEl = document.getElementById('cam-fps');
    const fmtEl = document.getElementById('cam-format');

    if (resEl) resEl.textContent = info.resolution || '—';
    if (fpsEl) fpsEl.textContent = info.fps ? `${info.fps}fps` : '—';
    if (fmtEl) fmtEl.textContent = info.format || '—';

    // Update header badge to show capture card indicator
    const badge = document.getElementById('badge-cam');
    if (badge && info.is_capture_card) {
        badge.innerHTML = '<span class="dot"></span> 📹 Capture Card';
    } else if (badge) {
        badge.innerHTML = '<span class="dot"></span> Camera';
    }
}

// ─── Device Scanning ─────────────────────────────────────────

async function scanDevices() {
    const btn = document.getElementById('btn-scan');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳';
    }

    try {
        const res = await fetch('/api/scan_devices');
        const data = await res.json();
        const select = document.getElementById('select-camera');
        if (!select) return;

        // Remember current selection
        const currentVal = select.value;

        // Clear and repopulate
        select.innerHTML = '<option value="auto">🔍 Auto-detect</option>';

        if (data.devices && data.devices.length > 0) {
            data.devices.forEach(dev => {
                const opt = document.createElement('option');
                opt.value = dev.index;
                const icon = dev.is_capture_card ? '📹' : '🎥';
                const res = dev.resolutions.length > 0 ? ` (${dev.resolutions[0]})` : '';
                opt.textContent = `${icon} ${dev.name}${res}`;
                select.appendChild(opt);
            });
        }

        // Restore selection if still valid
        const options = Array.from(select.options).map(o => o.value);
        if (options.includes(currentVal)) {
            select.value = currentVal;
        }
    } catch (e) {
        console.error('[Dashboard] Device scan failed:', e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔍';
        }
    }
}

// ─── Socket.IO Event Handlers ────────────────────────────────

socket.on('connect', () => {
    console.log('[Dashboard] Connected to server');
    // Scan devices on connect
    scanDevices();
});

socket.on('device_status', (data) => {
    const { device, ok, msg } = data;
    let badge = null;
    let infoEl = null;
    let overlayEl = null;

    if (device === 'camera') {
        badge = document.getElementById('badge-cam');
        infoEl = document.getElementById('cam-info');
        overlayEl = document.getElementById('cam-overlay');
        if (ok) {
            // Camera confirmed OK by backend — hide overlay immediately
            if (overlayEl) overlayEl.style.display = 'none';
        } else if (msg && (msg.toLowerCase().includes('lost') || msg.toLowerCase().includes('failed') || msg.toLowerCase().includes('gave up'))) {
            // Only show overlay for real errors, NOT for transient 'Connecting...' messages
            if (overlayEl) overlayEl.style.display = 'flex';
        }
        // Do NOT show overlay for 'Connecting...' — video may already be streaming
    } else if (device === 'oximeter') {
        badge = document.getElementById('badge-oxi');
        infoEl = document.getElementById('oxi-info');
    } else if (device === 'csi') {
        badge = document.getElementById('badge-csi');
        infoEl = document.getElementById('csi-info');
    } else if (device === 'emg') {
        badge = document.getElementById('badge-emg');
        infoEl = document.getElementById('emg-info');
    } else if (device === 'gsr') {
        badge = document.getElementById('badge-gsr');
        infoEl = document.getElementById('gsr-info');
    }

    if (badge) {
        badge.className = 'badge ' + (ok ? 'connected' : 'disconnected');
    }
    if (infoEl && msg) {
        infoEl.textContent = msg;
    }
});

socket.on('camera_info', (info) => {
    updateCameraInfo(info);
});

socket.on('oxi_data', (data) => {
    const { spo2, hr, wave, sig, n } = data;

    // No finger detected — show dashes
    if (sig === 0 || (spo2 === 0 && hr === 0)) {
        document.getElementById('spo2-val').textContent = '--';
        document.getElementById('hr-val').textContent = '--';
        setGauge('spo2-arc', 0, 100);
        setGauge('hr-arc', 0, 200);
        document.getElementById('oxi-count').textContent = `Samples: ${n}`;
        document.getElementById('oxi-signal').textContent = `Signal: No finger`;
        return;
    }

    // Update gauges
    animateValue('spo2-val', spo2);
    animateValue('hr-val', hr);
    setGauge('spo2-arc', spo2, 100);
    setGauge('hr-arc', Math.min(hr, 200), 200);

    // Update waveform chart (Disabled to prevent browser freezing)
    /*
    waveData.push(wave);
    waveLabels.push('');
    if (waveData.length > WAVE_MAX) {
        waveData.shift();
        waveLabels.shift();
    }
    waveChart.update();
    */

    // Update footer
    document.getElementById('oxi-count').textContent = `Samples: ${n}`;
    const sigBars = '█'.repeat(Math.min(sig, 7)) + '░'.repeat(Math.max(0, 7 - sig));
    document.getElementById('oxi-signal').textContent = `Signal: ${sigBars}`;
});

socket.on('csi_data', (data) => {
    const { amps, rssi, n } = data;

    if (amps && amps.length > 0) {
        csiAmps = amps;
        csiChart.data.datasets[0].data = csiAmps;
        csiChart.data.labels = Array.from({ length: amps.length }, (_, i) => i);
        csiChart.update();
    }

    document.getElementById('csi-rssi').textContent = `${rssi} dBm`;
    document.getElementById('csi-count').textContent = `Packets: ${n}`;
});

socket.on('emg_data', (data) => {
    const { channels, n } = data;
    if (channels && channels.length === 16) {
        emgChart.data.datasets[0].data = channels;
        emgChart.update();
    }
    document.getElementById('emg-count').textContent = `Packets: ${n}`;
});

socket.on('gsr_data', (data) => {
    const { uS, raw, stress, zscore, n, cal_progress } = data;
    
    // Calibration phase: stress === -1
    if (stress === -1 || stress === undefined) {
        const pct = cal_progress !== undefined ? cal_progress : 0;
        document.getElementById('stress-val').textContent = '⏳';
        document.getElementById('stress-val').style.fontSize = '1rem';
        setGauge('stress-arc', pct, 100);
        document.getElementById('gsr-us').textContent = Number(uS || 0).toFixed(2);
        document.getElementById('gsr-zscore').textContent = 'Cal...';
        document.getElementById('gsr-count').textContent = `Calibrating: ${pct}%`;
        return;
    }
    
    // Restore font size after calibration
    document.getElementById('stress-val').style.fontSize = '';

    // Update gauge
    animateValue('stress-val', Math.round(stress));
    setGauge('stress-arc', Math.max(0, stress), 100);

    // Update text
    document.getElementById('gsr-us').textContent = Number(uS).toFixed(2);
    document.getElementById('gsr-zscore').textContent = Number(zscore).toFixed(2);
    document.getElementById('gsr-count').textContent = `Samples: ${n}`;
});

socket.on('rec_started', (data) => {
    isRecording = true;
    recStartTime = Date.now();
    document.getElementById('session-label').textContent = `Session: ${data.session}`;
    document.getElementById('btn-start').disabled = true;
    document.getElementById('btn-stop').disabled = false;
    document.getElementById('rec-indicator').classList.remove('hidden');
    document.getElementById('progress-container').style.display = 'block';
    startTimer();
});

socket.on('rec_stopped', (data) => {
    isRecording = false;
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-stop').disabled = true;
    document.getElementById('rec-indicator').classList.add('hidden');
    stopTimer();

    const msg = `✅ ${data.session} — ${data.frames} frames, ${data.oxi} oxi samples, ${data.csi} CSI packets`;
    document.getElementById('session-label').textContent = msg;
    document.getElementById('progress-bar').style.width = '100%';
    setTimeout(() => {
        document.getElementById('progress-container').style.display = 'none';
        document.getElementById('progress-bar').style.width = '0%';
    }, 3000);
});

socket.on('record_format_changed', (data) => {
    const select = document.getElementById('select-format');
    if (select && data.format) {
        select.value = data.format;
    }
});

// ─── Recording Controls ─────────────────────────────────────

function startRecording() {
    const subject = document.getElementById('input-subject').value || 'unknown';
    const duration = parseInt(document.getElementById('input-duration').value) || 60;
    const recordFormat = document.getElementById('select-format').value || 'video';
    recDuration = duration;
    socket.emit('start_rec', { subject, duration, record_format: recordFormat });
}

function stopRecording() {
    socket.emit('stop_rec', {});
}

function startTimer() {
    if (timerInterval) clearInterval(timerInterval);
    timerInterval = setInterval(() => {
        if (!recStartTime) return;
        const elapsed = Math.floor((Date.now() - recStartTime) / 1000);
        const mins = Math.floor(elapsed / 60).toString().padStart(2, '0');
        const secs = (elapsed % 60).toString().padStart(2, '0');
        document.getElementById('rec-timer').textContent = `${mins}:${secs}`;

        // Progress bar
        if (recDuration > 0) {
            const pct = Math.min((elapsed / recDuration) * 100, 100);
            document.getElementById('progress-bar').style.width = `${pct}%`;
        }
    }, 500);
}

function stopTimer() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
}

// ─── Camera Source Switching ─────────────────────────────────

const cameraSelect = document.getElementById('select-camera');
if (cameraSelect) {
    cameraSelect.addEventListener('change', () => {
        const source = cameraSelect.value;
        socket.emit('switch_camera', { source });
        // Reset camera feed to trigger reconnect
        const feed = document.getElementById('camera-feed');
        if (feed) {
            feed.src = '';
            setTimeout(() => { feed.src = '/video_feed'; }, 500);
        }
    });
}

// ─── Record Format Switching ─────────────────────────────────

const formatSelect = document.getElementById('select-format');
if (formatSelect) {
    formatSelect.addEventListener('change', () => {
        const fmt = formatSelect.value;
        socket.emit('set_record_format', { format: fmt });
    });
}

// ─── Periodic Status Poll ────────────────────────────────────
// Reliable fallback: polls /api/status to keep badges and counters
// in sync even if SocketIO events were missed (e.g., late connect)

function updateBadge(id, ok) {
    const el = document.getElementById(id);
    if (el) el.className = 'badge ' + (ok ? 'connected' : 'disconnected');
}

statusInterval = setInterval(async () => {
    try {
        const res = await fetch('/api/status');
        const d = await res.json();

        // Update badges from polled state
        updateBadge('badge-cam', d.cam_ok);
        updateBadge('badge-oxi', d.oxi_ok);
        updateBadge('badge-csi', d.csi_ok);
        updateBadge('badge-emg', d.emg_ok);
        updateBadge('badge-gsr', d.gsr_ok);

        // Update card meta descriptions
        if (d.sensors) {
            const updateInfo = (id, info) => {
                const el = document.getElementById(id);
                if (el && info && info.status_msg) {
                    el.textContent = info.status_msg;
                }
            };
            updateInfo('cam-info', d.sensors.camera);
            updateInfo('oxi-info', d.sensors.oximeter);
            updateInfo('csi-info', d.sensors.csi);
            updateInfo('emg-info', d.sensors.emg);
            updateInfo('gsr-info', d.sensors.gsr);
        }

        // Update counters
        document.getElementById('cam-count').textContent = `Frames: ${d.cam_frames || 0}`;
        if (d.oxi_samples > 0)
            document.getElementById('oxi-count').textContent = `Samples: ${d.oxi_samples}`;
        if (d.csi_packets > 0)
            document.getElementById('csi-count').textContent = `Packets: ${d.csi_packets}`;
        if (d.emg_packets > 0)
            document.getElementById('emg-count').textContent = `Packets: ${d.emg_packets}`;
        if (d.gsr_samples > 0)
            document.getElementById('gsr-count').textContent = `Samples: ${d.gsr_samples}`;

        // Camera overlay
        const overlay = document.getElementById('cam-overlay');
        if (d.cam_ok && overlay) {
            overlay.style.display = 'none';
        }

        // Sync record format select
        if (d.record_format) {
            const fmtSel = document.getElementById('select-format');
            if (fmtSel && fmtSel.value !== d.record_format) {
                fmtSel.value = d.record_format;
            }
        }
    } catch (_) {}
}, 2000);

// ─── Camera MJPEG live-stream detection ──────────────────────
// Strategy: use a hidden canvas to sample pixels from the <img>.
// For a live MJPEG stream the browser continuously updates the img
// element's pixel data. As soon as we get non-zero pixel data we
// know real frames are arriving and we hide the overlay.
const camFeed = document.getElementById('camera-feed');
const camOverlay = document.getElementById('cam-overlay');
const _probeCanvas = document.createElement('canvas');
_probeCanvas.width = 4;
_probeCanvas.height = 4;
const _probeCtx = _probeCanvas.getContext('2d', { willReadFrequently: true });

function _hideCamOverlay() {
    if (camOverlay) {
        camOverlay.style.display = 'none';
    }
    const badge = document.getElementById('badge-cam');
    if (badge) badge.className = 'badge connected';
}

function _showCamOverlay() {
    if (camOverlay) {
        camOverlay.style.display = 'flex';
    }
    const badge = document.getElementById('badge-cam');
    if (badge) badge.className = 'badge disconnected';
}

function _probeImagePixels() {
    if (!camFeed || camFeed.naturalWidth === 0) return false;
    try {
        _probeCtx.drawImage(camFeed, 0, 0, 4, 4);
        const px = _probeCtx.getImageData(0, 0, 4, 4).data;
        // Check if any pixel is non-zero (all-black = still connecting)
        for (let i = 0; i < px.length; i++) {
            if (px[i] > 10) return true;  // Real image content detected
        }
        return false;
    } catch (e) {
        // CORS or cross-origin error: if we can't read it, it means
        // the browser HAS loaded a frame (cross-origin policy triggers
        // only after successful load)
        return true;
    }
}

// Method 1: load event (fires for static img, sometimes for MJPEG first frame)
camFeed.addEventListener('load', _hideCamOverlay);

// Method 2: error → show overlay if stream is gone
camFeed.addEventListener('error', () => {
    if (camOverlay && camOverlay.style.display !== 'none') return; // already hidden, ignore
    _showCamOverlay();
});

// Method 3: Aggressive polling every 300ms — check both naturalWidth>0 AND pixel data
const _camPollTimer = setInterval(() => {
    // MJPEG img gets naturalWidth > 0 only after first full frame decoded
    if (camFeed.naturalWidth > 0) {
        _hideCamOverlay();
        return;
    }
    // Fallback: check via canvas pixel probe
    if (_probeImagePixels()) {
        _hideCamOverlay();
    }
}, 300);

// Method 4: Monitor /api/status for cam_ok — already handled in statusInterval above
// (already calls overlay.style.display = 'none' when d.cam_ok is true)


// ─── Init ────────────────────────────────────────────────────
console.log('[Dashboard] Dataset Sync Live Dashboard initialized');

// ─── Device Setup Checklist ──────────────────────────────────
function showSetupModal() {
    const modal = document.getElementById('setup-modal');
    if (modal) {
        modal.classList.remove('hidden');
        fetchSetupPorts();
    }
}

function closeSetupModal() {
    const modal = document.getElementById('setup-modal');
    if (modal) {
        modal.classList.add('hidden');
    }
}

async function fetchSetupPorts() {
    try {
        const resPorts = await fetch('/api/ports');
        const data = await resPorts.json();
        
        let activePorts = {};
        let isMonitoring = false;
        try {
            const resStatus = await fetch('/api/status');
            const status = await resStatus.json();
            isMonitoring = !!status.monitoring;
            if (isMonitoring && status.sensors) {
                activePorts = {
                    camera: status.sensors.camera?.port,
                    oximeter: status.sensors.oximeter?.port,
                    csi: status.sensors.csi?.port,
                    emg: status.sensors.emg?.port,
                    gsr: status.sensors.gsr?.port
                };
            }
        } catch (e) {
            console.error("Error fetching status:", e);
        }

        const closeBtn = document.getElementById('btn-close-setup');
        if (closeBtn) {
            if (isMonitoring) {
                closeBtn.classList.remove('hidden');
            } else {
                closeBtn.classList.add('hidden');
            }
        }
        
        const populate = (selectId, options, isVideo = false, activeVal = null) => {
            const select = document.getElementById(selectId);
            if (!select) return;
            select.innerHTML = '<option value="none">None (Disabled)</option>';
            options.forEach(opt => {
                const el = document.createElement('option');
                if (isVideo) {
                    const val = opt.path !== undefined && opt.path !== null ? opt.path : opt.index;
                    el.value = val;
                    el.textContent = `${opt.name} (${val})`;
                } else {
                    el.value = opt.device;
                    el.textContent = `${opt.device} - ${opt.description}`;
                }
                select.appendChild(el);
            });
            
            if (activeVal !== null && activeVal !== undefined) {
                const valStr = String(activeVal);
                if (valStr && valStr !== 'none') {
                    let exists = false;
                    for (let i = 0; i < select.options.length; i++) {
                        if (select.options[i].value === valStr) {
                            select.value = valStr;
                            exists = true;
                            break;
                        }
                    }
                    if (!exists) {
                        const el = document.createElement('option');
                        el.value = valStr;
                        el.textContent = `${valStr} (Current)`;
                        select.appendChild(el);
                        select.value = valStr;
                    }
                } else {
                    select.value = 'none';
                }
            } else {
                if (selectId === 'setup-csi' && options.find(o => o.device === '/dev/ttyUSB1')) select.value = '/dev/ttyUSB1';
            }
        };

        populate('setup-camera', data.video, true, activePorts.camera);
        populate('setup-oxi', data.serial, false, activePorts.oximeter);
        populate('setup-csi', data.serial, false, activePorts.csi);
        populate('setup-emg', data.serial, false, activePorts.emg);
        populate('setup-gsr', data.serial, false, activePorts.gsr);
        
    } catch (err) {
        console.error('Failed to fetch ports:', err);
    }
}

async function startSystem() {
    const config = {
        camera: document.getElementById('setup-camera').value,
        oximeter: document.getElementById('setup-oxi').value,
        csi: document.getElementById('setup-csi').value,
        emg: document.getElementById('setup-emg').value,
        gsr: document.getElementById('setup-gsr').value
    };

    // Validation: prevent assigning same serial port to multiple sensors
    const assigned = [config.oximeter, config.csi, config.emg, config.gsr].filter(v => v !== 'none');
    if (new Set(assigned).size !== assigned.length) {
        alert("Error: You cannot assign the same serial port to multiple sensors!");
        return;
    }

    try {
        const res = await fetch('/api/start_monitoring', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });
        
        if (res.ok) {
            document.getElementById('setup-modal').classList.add('hidden');
        } else {
            alert("Failed to start system.");
        }
    } catch (err) {
        console.error('Failed to start system:', err);
        alert("Network error.");
    }
}

// On page load, show the setup modal and populate the ports
document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('setup-modal');
    if (modal) {
        modal.classList.remove('hidden');
    }
    fetchSetupPorts();
});
