import time
import sqlite3
import threading
import firebase_admin
from firebase_admin import credentials, firestore
from zk import ZK
from datetime import datetime

# --- CONFIGURATION ---
DEVICE_IP = '192.168.0.111'  # UPDATE THIS with your K50's Static IP
DEVICE_PORT = 4370
DB_PATH = '/opt/zk-bridge/local_buffer.db'
FIREBASE_KEY = '/opt/zk-bridge/serviceAccountKey.json'
# ---------------------

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY)
    firebase_admin.initialize_app(cred)
db = firestore.client()

def init_local_db():
    """Creates the local SQLite buffer if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                timestamp TEXT,
                status INTEGER,
                punch_type INTEGER,
                synced INTEGER DEFAULT 0
            )
        ''')

def save_locally(user_id, timestamp, status, punch_type):
    """Saves raw data to local disk immediately (Millisecond latency)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO attendance (user_id, timestamp, status, punch_type) VALUES (?, ?, ?, ?)",
                (user_id, str(timestamp), status, punch_type)
            )
        print(f"💾 Buffered: User {user_id} at {timestamp}")
    except Exception as e:
        print(f"❌ Database Error: {e}")

def sync_to_firebase():
    """Background worker: Uploads unsynced records to Firestore."""
    print("☁️  Sync Agent Started...")
    while True:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Fetch 10 unsynced records
                cursor.execute("SELECT * FROM attendance WHERE synced = 0 LIMIT 10")
                rows = cursor.fetchall()

                if not rows:
                    time.sleep(5) # No data? Sleep to save CPU
                    continue

                for row in rows:
                    # Map ZKTeco Status to Business Logic
                    # K50 Status: 0=Check-In, 1=Check-Out (Verify on your device keys)
                    action = "check-in" if row['status'] == 0 else "check-out"
                    
                    doc_data = {
                        "userId": row['user_id'],
                        "timestamp": datetime.strptime(row['timestamp'], "%Y-%m-%d %H:%M:%S"),
                        "status": action,
                        "device_raw_status": row['status'],
                        "syncedAt": firestore.SERVER_TIMESTAMP
                    }

                    # Push to Firestore
                    db.collection('attendance').add(doc_data)
                    print(f"🚀 Uploaded: User {row['user_id']} ({action})")

                    # Mark as synced
                    conn.execute("UPDATE attendance SET synced = 1 WHERE id = ?", (row['id'],))
                    conn.commit()

        except Exception as e:
            print(f"⚠️ Sync Paused (Internet Issue?): {e}")
            time.sleep(10)

def run_device_listener():
    """Main Process: Keeps a persistent connection to the K50."""
    zk = ZK(DEVICE_IP, port=DEVICE_PORT, timeout=5, force_udp=False, ommit_ping=False)
    
    while True:
        try:
            print(f"🔌 Connecting to Device {DEVICE_IP}...")
            conn = zk.connect()
            print("✅ Connected. Listening for live punches...")
            
            # live_capture blocks here until a finger is pressed
            for attendance in conn.live_capture():
                if attendance is None:
                    continue
                
                # On punch detected: Save to disk IMMEDIATELY
                save_locally(
                    attendance.user_id, 
                    attendance.timestamp, 
                    attendance.status, 
                    attendance.punch
                )

        except Exception as e:
            print(f"❌ Device Connection Lost: {e}")
            print("🔄 Retrying in 10 seconds...")
            time.sleep(10)
        finally:
            try:
                conn.disconnect()
            except:
                pass

if __name__ == "__main__":
    # 1. Prepare DB
    init_local_db()
    
    # 2. Start Sync Agent (Daemon Thread)
    sync_thread = threading.Thread(target=sync_to_firebase, daemon=True)
    sync_thread.start()
    
    # 3. Start Device Listener (Blocking Main Thread)
    run_device_listener()
