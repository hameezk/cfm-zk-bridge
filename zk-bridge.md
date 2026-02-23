# ZKTeco to Firebase Attendance Bridge 🌉

This documentation outlines the architecture, deployment, and management of the Python-based middleware that connects a ZKTeco K50 biometric device to a Firebase backend.

## 1. System Architecture

The server operates as a persistent Linux daemon with three parallel processes:

1. **Device Listener (Real-Time):** Maintains a continuous TCP connection to the ZKTeco machine. Punches are instantly captured and buffered to a local SQLite database (`local_buffer.db`).
2. **User Sync (Cron Scheduler):** Runs daily at 6:00 PM to fetch the latest employees from the Firebase `users` collection. It dynamically maps Firebase IDs (e.g., `CFMQA023`) to the biometric device's integer IDs (`23`) and caches them locally.
3. **Cloud Sync (Background Worker):** Continuously scans the local buffer for unsynced attendance punches. It maps the IDs, calculates the correct "Business Day" (6:00 PM to 5:59 PM), and upserts the exact document format required by the Flutter frontend.

---

## 2. Prerequisites

Before deploying the bridge on your Linux device (Ubuntu/Debian/Raspberry Pi), ensure you have the following:

* Root (`sudo`) privileges.
* **Python 3.7+** installed (`python3 --version`).
* **Static IP** assigned to the ZKTeco K50 (e.g., `192.168.18.104`).
* The Firebase **serviceAccountKey.json**.

---

## 3. Installation & Setup

### Step 1: Prepare the Environment
Create a dedicated directory in `/opt` to host the bridge securely.
```bash
sudo mkdir -p /opt/zk-bridge
sudo chown -R $USER:$USER /opt/zk-bridge
cd /opt/zk-bridge
```

### Step 2: Transfer Required Files
Place the following two files into `/opt/zk-bridge/`:
* `firebase_agent.py`
* `serviceAccountKey.json`

### Step 3: Configure the Python Virtual Environment
Isolating dependencies ensures system updates won't break the script.
```bash
sudo apt update
sudo apt install python3-venv python3-pip -y

# Create and activate environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install firebase-admin pyzk

# Deactivate when done
deactivate
```

---

## 4. Making it Persistent (Systemd Daemon)

To guarantee the script runs 24/7, survives reboots, and automatically recovers from crashes, we will configure it as a `systemd` service.

### Step 1: Create the Service File
```bash
sudo nano /etc/systemd/system/zk-bridge.service
```

### Step 2: Add the Configuration
Paste the following into the nano editor:
```ini
[Unit]
Description=ZKTeco to Firebase Attendance Bridge
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/zk-bridge
ExecStart=/opt/zk-bridge/venv/bin/python /opt/zk-bridge/firebase_agent.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
*(Save and exit: `Ctrl+O`, `Enter`, `Ctrl+X`)*

### Step 3: Enable and Start the Daemon
Register the service with Linux and launch it.
```bash
sudo systemctl daemon-reload
sudo systemctl enable zk-bridge
sudo systemctl start zk-bridge
```

---

## 5. Monitoring & Troubleshooting

The server is entirely headless. You will use `systemctl` and `journalctl` to interact with it.

**Check Service Status:**
```bash
sudo systemctl status zk-bridge
```

**View Live Logs (Real-time output):**
```bash
sudo journalctl -u zk-bridge -f
```
*(Look for the `✅ Connected. Listening for live punches...` and `🚀 Synced...` messages to confirm operation).*

**Restarting the Service (After code updates):**
```bash
sudo systemctl restart zk-bridge
```

**Stopping the Service:**
```bash
sudo systemctl stop zk-bridge
```


