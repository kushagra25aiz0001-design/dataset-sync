"""
Viewer App — Optional GUI for Headless Mode
===========================================
A lightweight dashboard that reads state from `headless_daemon.py`.
It does NOT touch the serial ports. You can start/stop it freely
without affecting the background recording.

Usage:
    python -m src.dashboard.viewer_app
"""

import json
import os
import threading
import time

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
from src.recorder.ipc_server import IPC_FILE

BASE_DIR = os.path.dirname(__file__)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = 'viewer-only-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

_last_state = {}

def read_ipc_state():
    if not os.path.exists(IPC_FILE):
        return None
    try:
        with open(IPC_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return None

def state_broadcaster():
    """Reads IPC file and emits changes to SocketIO clients."""
    global _last_state
    while True:
        state = read_ipc_state()
        if state:
            # Emit oximeter data if present
            oxi = state.get('latest_oxi')
            if oxi:
                socketio.emit('oxi_data', oxi)

            # Emit device statuses
            sensors = state.get('sensors', {})
            for name, info in sensors.items():
                old_info = _last_state.get('sensors', {}).get(name, {})
                # If state changed, update UI
                if info.get('state') != old_info.get('state') or info.get('status_msg') != old_info.get('status_msg'):
                    socketio.emit('device_status', {
                        'device': name,
                        'ok': info.get('ok', False),
                        'msg': info.get('status_msg', ''),
                    })
            
            _last_state = state
        time.sleep(0.5)

@app.route('/')
def index():
    return render_template('dashboard.html', viewer_only=True)

@app.route('/api/status')
def api_status():
    state = read_ipc_state()
    return jsonify(state or {})

@socketio.on('connect')
def handle_connect():
    # Push latest status immediately to new client
    state = read_ipc_state()
    if state:
        sensors = state.get('sensors', {})
        for name, info in sensors.items():
            socketio.emit('device_status', {
                'device': name,
                'ok': info.get('ok', False),
                'msg': info.get('status_msg', ''),
            })

if __name__ == '__main__':
    print('=' * 60)
    print('  📊 Viewer App for Headless Daemon')
    print('  Open http://localhost:5001 in your browser')
    print('=' * 60)
    threading.Thread(target=state_broadcaster, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5001, debug=False, allow_unsafe_werkzeug=True)
