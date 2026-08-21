from datetime import datetime, timedelta
from email.mime.text import MIMEText
import os
import smtplib
import sqlite3
import threading
import time

from flask import Flask, jsonify, render_template_string, request
from tzlocal import get_localzone

# --- AUTOMATIC SYSTEM TIMEZONE DETECTION ---
LOCAL_TZ = get_localzone()
TIMEZONE_NAME = str(LOCAL_TZ)

os.environ["TZ"] = TIMEZONE_NAME
if hasattr(time, "tzset"):
    time.tzset()

app = Flask(__name__)

# --- CONFIGURATION ---
DB_FILE = "tanks.db"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SENDER_EMAIL   "your@gmail.com"  # Sender email address for alerts
SENDER_PASSWORD   "xxxx xxxx xxxx xxxx"  # SMTP App Password
RECIPIENT_EMAIL   "your@gmail.com"  # Alert notification recipient

last_alert_time = {"tank1": None, "tank2": None}


# --- DATABASE INITIALIZATION ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tank_id TEXT NOT NULL,
                        distance REAL NOT NULL,
                        battery REAL DEFAULT 0.0,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value REAL NOT NULL
                    )"""
        )
        c.execute(
            "INSERT OR IGNORE INTO settings VALUES ('threshold_tank1', 50.0)"
        )
        c.execute(
            "INSERT OR IGNORE INTO settings VALUES ('threshold_tank2', 50.0)"
        )

        try:
            c.execute("ALTER TABLE readings ADD COLUMN battery REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass

        conn.commit()


init_db()


def get_threshold(tank_id):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT value FROM settings WHERE key = ?",
            (f"threshold_{tank_id}",),
        )
        row = c.fetchone()
        return row[0] if row else 50.0


def set_threshold_db(tank_id, value):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "REPLACE INTO settings (key, value) VALUES (?, ?)",
            (f"threshold_{tank_id}", value),
        )
        conn.commit()


# --- EMAIL WORKER ---
def send_email_alert(tank_id, distance, threshold):
    now_local_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

    subject = f"CRITICAL: Water Level Alert - {tank_id.upper()}"
    body = (
        f"Water level alert for {tank_id.upper()}:\n\n"
        f"• Current Distance: {distance:.1f} cm\n"
        f"• Alert Threshold:  {threshold:.1f} cm\n"
        f"• Timestamp:        {now_local_str} ({TIMEZONE_NAME})\n\n"
        f"Action required: Tank level has dropped below safety threshold."
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        print(f"[{tank_id}] Alert email sent in background thread.")
    except Exception as e:
        print(f"[{tank_id}] Failed to send background email: {e}")


# --- API ENDPOINTS ---
@app.route("/api/reading", methods=["POST"])
def receive_reading():
    data = request.get_json(silent=True) or {}
    tank_id = data.get("tank_id")
    try:
        distance = float(data.get("distance", 0.0))
        battery = float(data.get("battery", 0.0))
    except (ValueError, TypeError):
        distance, battery = 0.0, 0.0

    if tank_id not in ["tank1", "tank2"]:
        return jsonify({"status": "error", "message": "Invalid tank_id"}), 400

    local_now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO readings (tank_id, distance, battery, timestamp) VALUES (?, ?, ?, ?)",
            (tank_id, distance, battery, local_now_str),
        )
        conn.commit()

    threshold = get_threshold(tank_id)

    if distance >= threshold and distance > 0:
        now_local = datetime.now(LOCAL_TZ)
        last_sent = last_alert_time[tank_id]

        if last_sent is None or (now_local - last_sent) >= timedelta(
            hours=24
        ):
            threading.Thread(
                target=send_email_alert, args=(tank_id, distance, threshold)
            ).start()
            last_alert_time[tank_id] = now_local

    return jsonify({"status": "success", "threshold": threshold}), 200


@app.route("/api/status", methods=["GET"])
def get_status():
    readings = {}
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        for t in ["tank1", "tank2"]:
            c.execute(
                "SELECT distance, battery, timestamp FROM readings WHERE tank_id = ? ORDER BY id DESC LIMIT 1",
                (t,),
            )
            row = c.fetchone()
            readings[t] = {
                "distance": row[0] if row else 0.0,
                "battery": row[1] if row else 0.0,
                "timestamp": row[2] if row else "No readings yet",
            }

    return jsonify(
        {
            "readings": readings,
            "thresholds": {
                "tank1": get_threshold("tank1"),
                "tank2": get_threshold("tank2"),
            },
            "last_alert_sent": {
                k: (v.strftime("%Y-%m-%d %H:%M:%S") if v else "Never")
                for k, v in last_alert_time.items()
            },
        }
    )


@app.route("/api/threshold", methods=["POST"])
def update_threshold():
    data = request.get_json(silent=True) or {}
    tank_id = data.get("tank_id")
    threshold = data.get("threshold")

    if tank_id in ["tank1", "tank2"] and threshold is not None:
        try:
            val = float(threshold)
            set_threshold_db(tank_id, val)
            return jsonify({"status": "success", "threshold": val})
        except ValueError:
            pass
    return jsonify({"status": "error", "message": "Invalid input"}), 400


@app.route("/api/history", methods=["GET"])
def get_history():
    time_range = request.args.get("range", "1h")
    now_local = datetime.now(LOCAL_TZ)

    if time_range == "1h":
        cutoff = now_local - timedelta(hours=1)
        group_fmt = "%H:%M"
    elif time_range == "1w":
        cutoff = now_local - timedelta(days=7)
        group_fmt = "%m-%d %H:00"
    elif time_range == "1m":
        cutoff = now_local - timedelta(days=30)
        group_fmt = "%m-%d %H:00"
    elif time_range == "1y":
        cutoff = now_local - timedelta(days=365)
        group_fmt = "%Y-%m-%d"
    else:
        cutoff = now_local - timedelta(days=1)
        group_fmt = "%H:%M"

    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        query = f"""
            SELECT 
                strftime('{group_fmt}', timestamp) as time_group,
                tank_id,
                AVG(distance) as avg_dist,
                AVG(CASE WHEN battery > 0 THEN battery ELSE NULL END) as avg_batt
            FROM readings 
            WHERE timestamp >= ?
            GROUP BY time_group, tank_id 
            ORDER BY timestamp ASC
        """
        c.execute(query, (cutoff_str,))
        rows = c.fetchall()

    timeline = {}
    for time_group, tank_id, avg_dist, avg_batt in rows:
        if time_group not in timeline:
            timeline[time_group] = {
                "tank1": None,
                "tank2": None,
                "battery": None,
            }

        if tank_id in ["tank1", "tank2"]:
            timeline[time_group][tank_id] = (
                round(avg_dist, 1) if avg_dist is not None else None
            )

        if avg_batt is not None and timeline[time_group]["battery"] is None:
            timeline[time_group]["battery"] = round(avg_batt, 2)

    labels = list(timeline.keys())
    tank1_data = [timeline[t]["tank1"] for t in labels]
    tank2_data = [timeline[t]["tank2"] for t in labels]
    battery_data = [timeline[t]["battery"] for t in labels]

    return jsonify(
        {
            "labels": labels,
            "tank1": tank1_data,
            "tank2": tank2_data,
            "battery": battery_data,
        }
    )


# --- DASHBOARD HTML ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Water Tank Monitor & History</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='1em' height='1em' viewBox='0 0 512 512'><path d='M0 0h512v512H0z' fill='none'/><path fill='%2300bcff' d='M160 25c-14.5 0-38 3.89-56.7 11.36c-9.29 3.73-17.41 8.37-22.69 13.25A30 30 0 0 0 75.99 55H244c-1.2-1.77-2.6-3.55-4.6-5.39c-5.3-4.88-13.4-9.52-22.7-13.25C198 28.89 174.5 25 160 25M89 73v129.9l71 42.6l71-42.6V73zm167 78v18h23v14h-30v18h51.3l29.3 29.4l12.8-12.8l-34.7-34.6H297v-14h23v-18zM84.65 221.3L39.12 494.5l17.76 3L74.3 393h171.4l17.4 104.5l17.8-3l-45.5-273.2l-16.7 10l13.7 81.8l-72.4 26.4l-72.38-26.3l13.68-81.9zb263.15 22.4s-10.2 49.6 5.2 59.7c9.8 6.4 28.8-2.9 31-15.4c3.8-19.6-36.2-44.3-36.2-44.3M377 329s-13.4 29.1-5.8 38.5c4.9 5.9 17.4 3.7 20.6-3.6C397 352.3 377 329 377 329m-292.41 2.3l48.91 17.8l-55.22 20.1zm150.81 0l6.3 37.9l-55.2-20.1zm179 7.5s1.4 32.1 12.4 36.9c7.2 3 17.2-4.7 16.8-12.7c-.8-12.6-29.2-24.2-29.2-24.2M160 358.7l44.8 16.3h-89.6zm251.7 40.5s-3.4 21.5 3.4 26.2c4.3 2.9 12.1-1 12.8-6.3c1.3-8.6-16.2-19.9-16.2-19.9'/></svg>">
</head>
<body class="bg-slate-900 text-white min-h-screen p-6">
    <div class="max-w-6xl mx-auto space-y-6">
        
        <header class="border-b border-slate-800 pb-4 flex justify-between items-center">
            <div>
                <h1 class="text-3xl font-bold text-sky-400">Water Tank System</h1>
                <p class="text-slate-400 text-sm">Real-time Level Control & Battery Status</p>
            </div>
            <div class="flex items-center gap-4">
                <span id="battery-status" class="px-3 py-1 bg-slate-800 text-amber-400 border border-slate-700 rounded-full text-xs font-semibold">
                    ⚡ Batt: --V
                </span>
                <span id="live-indicator" class="px-3 py-1 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full text-xs font-semibold">
                    ● Connected
                </span>
            </div>
        </header>

        <!-- CARDS SECTION -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <!-- TANK 1 -->
            <div class="bg-slate-800 rounded-xl p-6 border border-slate-700 shadow-xl space-y-4">
                <div class="flex justify-between items-center">
                    <h2 class="text-xl font-bold text-slate-200">Tank 1</h2>
                    <span id="t1-badge" class="px-2.5 py-1 rounded text-xs font-semibold bg-slate-700 text-slate-300">--</span>
                </div>
                <div>
                    <p class="text-slate-400 text-xs">CURRENT DISTANCE</p>
                    <p id="t1-dist" class="text-4xl font-black text-white">-- cm</p>
                </div>
                <div class="flex items-center gap-2 bg-slate-900/50 p-3 rounded-lg border border-slate-700/50">
                    <div class="flex-1">
                        <label class="text-slate-400 text-xs block">ALERT THRESHOLD (CM)</label>
                        <input id="t1-input" type="number" step="0.5" class="bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm w-full text-white font-bold mt-1">
                    </div>
                    <button onclick="updateThreshold('tank1')" class="mt-4 px-3 py-1.5 bg-sky-600 hover:bg-sky-500 text-xs font-bold rounded transition">Save</button>
                </div>
                <div class="text-xs text-slate-500 flex justify-between">
                    <span id="t1-email">Last Email: Never</span>
                    <span id="t1-time">Updated: Never</span>
                </div>
            </div>

            <!-- TANK 2 -->
            <div class="bg-slate-800 rounded-xl p-6 border border-slate-700 shadow-xl space-y-4">
                <div class="flex justify-between items-center">
                    <h2 class="text-xl font-bold text-slate-200">Tank 2</h2>
                    <span id="t2-badge" class="px-2.5 py-1 rounded text-xs font-semibold bg-slate-700 text-slate-300">--</span>
                </div>
                <div>
                    <p class="text-slate-400 text-xs">CURRENT DISTANCE</p>
                    <p id="t2-dist" class="text-4xl font-black text-white">-- cm</p>
                </div>
                <div class="flex items-center gap-2 bg-slate-900/50 p-3 rounded-lg border border-slate-700/50">
                    <div class="flex-1">
                        <label class="text-slate-400 text-xs block">ALERT THRESHOLD (CM)</label>
                        <input id="t2-input" type="number" step="0.5" class="bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm w-full text-white font-bold mt-1">
                    </div>
                    <button onclick="updateThreshold('tank2')" class="mt-4 px-3 py-1.5 bg-sky-600 hover:bg-sky-500 text-xs font-bold rounded transition">Save</button>
                </div>
                <div class="text-xs text-slate-500 flex justify-between">
                    <span id="t2-email">Last Email: Never</span>
                    <span id="t2-time">Updated: Never</span>
                </div>
            </div>
        </div>

        <!-- GRAPH SECTION -->
        <div class="bg-slate-800 rounded-xl p-6 border border-slate-700 shadow-xl space-y-4">
            <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 border-b border-slate-700 pb-4">
                <h2 class="text-lg font-bold text-slate-200">Historical Distance & Battery Trends</h2>
                <div class="flex bg-slate-900 p-1 rounded-lg border border-slate-700 text-xs">
                    <button onclick="setRange('1h')" id="btn-1h" class="range-btn px-3 py-1.5 rounded font-semibold bg-sky-600 text-white">1 Hour</button>
                    <button onclick="setRange('1d')" id="btn-1d" class="range-btn px-3 py-1.5 rounded font-semibold text-slate-400 hover:text-white">1 Day</button>
                    <button onclick="setRange('1w')" id="btn-1w" class="range-btn px-3 py-1.5 rounded font-semibold text-slate-400 hover:text-white">1 Week</button>
                    <button onclick="setRange('1m')" id="btn-1m" class="range-btn px-3 py-1.5 rounded font-semibold text-slate-400 hover:text-white">1 Month</button>
                    <button onclick="setRange('1y')" id="btn-1y" class="range-btn px-3 py-1.5 rounded font-semibold text-slate-400 hover:text-white">1 Year</button>
                </div>
            </div>
            <div class="relative h-80 w-full">
                <canvas id="tankChart"></canvas>
            </div>
        </div>

    </div>

    <script>
        let chart;
        let currentRange = '1h';
        let lastTimestampTank1 = "";
        let lastTimestampTank2 = "";

        function initChart() {
            const ctx = document.getElementById('tankChart').getContext('2d');
            chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        { 
                            label: 'Tank 1 (cm)', 
                            data: [], 
                            yAxisID: 'y',
                            borderColor: '#38bdf8', 
                            backgroundColor: 'rgba(56, 189, 248, 0.15)', 
                            pointRadius: 4, pointHoverRadius: 7, pointBackgroundColor: '#38bdf8', pointBorderColor: '#ffffff', pointBorderWidth: 1.5, tension: 0.2, fill: true, spanGaps: true 
                        },
                        { 
                            label: 'Tank 2 (cm)', 
                            data: [], 
                            yAxisID: 'y',
                            borderColor: '#a855f7', 
                            backgroundColor: 'rgba(168, 85, 247, 0.15)', 
                            pointRadius: 4, pointHoverRadius: 7, pointBackgroundColor: '#a855f7', pointBorderColor: '#ffffff', pointBorderWidth: 1.5, tension: 0.2, fill: true, spanGaps: true 
                        },
                        { 
                            label: 'Battery (V)', 
                            data: [], 
                            yAxisID: 'y1',
                            borderColor: '#f59e0b', 
                            backgroundColor: 'transparent',
                            borderDash: [4, 4],
                            pointRadius: 3, pointHoverRadius: 6, pointBackgroundColor: '#f59e0b', pointBorderColor: '#ffffff', pointBorderWidth: 1, tension: 0.2, fill: false, spanGaps: true 
                        }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    scales: {
                        x: { grid: { color: '#334155' }, ticks: { color: '#94a3b8' } },
                        y: { 
                            type: 'linear', display: true, position: 'left',
                            grid: { color: '#334155' }, ticks: { color: '#94a3b8' }, 
                            title: { display: true, text: 'Distance (cm)', color: '#94a3b8' } 
                        },
                        y1: { 
                            type: 'linear', display: true, position: 'right',
                            grid: { drawOnChartArea: false }, ticks: { color: '#f59e0b' }, 
                            title: { display: true, text: 'Battery (Volts)', color: '#f59e0b' } 
                        }
                    },
                    plugins: { legend: { labels: { color: '#e2e8f0' } } }
                }
            });
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                let hasNewData = false;

                ['tank1', 'tank2'].forEach(tank => {
                    const prefix = tank === 'tank1' ? 't1' : 't2';
                    const reading = data.readings[tank];
                    const thresh = data.thresholds[tank];

                    if (tank === 'tank1' && reading.timestamp !== lastTimestampTank1) {
                        if (lastTimestampTank1 !== "") hasNewData = true;
                        lastTimestampTank1 = reading.timestamp;
                    } else if (tank === 'tank2' && reading.timestamp !== lastTimestampTank2) {
                        if (lastTimestampTank2 !== "") hasNewData = true;
                        lastTimestampTank2 = reading.timestamp;
                    }

                    document.getElementById(`${prefix}-dist`).innerText = `${reading.distance.toFixed(1)} cm`;
                    if (document.activeElement !== document.getElementById(`${prefix}-input`)) {
                        document.getElementById(`${prefix}-input`).value = thresh;
                    }
                    document.getElementById(`${prefix}-email`).innerText = `Last Email: ${data.last_alert_sent[tank]}`;
                    document.getElementById(`${prefix}-time`).innerText = `Updated: ${reading.timestamp}`;

                    const badge = document.getElementById(`${prefix}-badge`);
                    if (reading.distance === 0) {
                        badge.innerText = "NO DATA";
                        badge.className = "px-2.5 py-1 rounded text-xs font-semibold bg-gray-500/20 text-gray-400";
                    } else if (reading.distance >= thresh) {
                        badge.innerText = "CRITICAL LOW";
                        badge.className = "px-2.5 py-1 rounded text-xs font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/30";
                    } else {
                        badge.innerText = "NORMAL";
                        badge.className = "px-2.5 py-1 rounded text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
                    }
                });

                const latestBatt = data.readings.tank1.battery || data.readings.tank2.battery || 0;
                document.getElementById('battery-status').innerText = `⚡ Batt: ${latestBatt.toFixed(2)}V`;

                if (hasNewData) {
                    fetchHistory();
                }

            } catch (e) { console.error("Error updating status:", e); }
        }

        async function fetchHistory() {
            try {
                const res = await fetch(`/api/history?range=${currentRange}`);
                const data = await res.json();

                chart.data.labels = data.labels;
                chart.data.datasets[0].data = data.tank1;
                chart.data.datasets[1].data = data.tank2;
                chart.data.datasets[2].data = data.battery;
                chart.update();
            } catch (e) { console.error("Error updating history chart:", e); }
        }

        async function updateThreshold(tankId) {
            const prefix = tankId === 'tank1' ? 't1' : 't2';
            const val = parseFloat(document.getElementById(`${prefix}-input`).value);

            const res = await fetch('/api/threshold', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tank_id: tankId, threshold: val })
            });

            if (res.ok) {
                alert(`Threshold for ${tankId} set to ${val} cm`);
                fetchStatus();
            }
        }

        function setRange(range) {
            currentRange = range;
            document.querySelectorAll('.range-btn').forEach(btn => {
                btn.className = "range-btn px-3 py-1.5 rounded font-semibold text-slate-400 hover:text-white";
            });
            document.getElementById(`btn-${range}`).className = "range-btn px-3 py-1.5 rounded font-semibold bg-sky-600 text-white";
            fetchHistory();
        }

        initChart();
        fetchStatus();
        fetchHistory();
        setInterval(fetchStatus, 3000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
