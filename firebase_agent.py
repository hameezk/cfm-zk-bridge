import time
import sqlite3
import threading
import firebase_admin
from firebase_admin import credentials, firestore
from zk import ZK
from datetime import datetime, timedelta

# --- CONFIGURATION ---
DEVICE_IP = '192.168.18.104'  
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
    """Creates the local SQLite buffer and Users cache if they don't exist."""
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                device_id TEXT PRIMARY KEY,
                firebase_id TEXT,
                name TEXT,
                shift_timing TEXT
            )
        ''')

def sync_users_from_firebase():
    """Fetches users from Firestore, extracts IDs, and caches them locally."""
    print("🔄 Fetching users from Firebase...")
    try:
        users_ref = db.collection('users').stream()
        
        with sqlite3.connect(DB_PATH) as conn:
            # Clear old mappings to prevent stale ID data
            conn.execute("DELETE FROM users")
            
            for doc in users_ref:
                data = doc.to_dict()
                firebase_doc_id = doc.id 
                emp_id_field = data.get("employeeId") # Pulling from the field, not doc.id
                
                if not emp_id_field:
                    print(f"⚠️ Skipping: User document {firebase_doc_id} has no 'employeeId' field.")
                    continue
                
                try:
                    # Clean the employeeId (removes spaces/special chars) and get digits
                    # We take the last 3 digits, then convert to int to drop leading zeros
                    # Example: "CFM-0022" -> "022" -> 22 -> "22"
                    # Example: "111" -> "111" -> 111 -> "111"
                    
                    suffix = str(emp_id_field)[-3:] 
                    device_id = str(int(suffix)) 
                    
                    name = data.get('name', 'Unknown')
                    shift_timing = data.get('shiftTiming', '')
                    
                    conn.execute('''
                        INSERT OR REPLACE INTO users (device_id, firebase_id, name, shift_timing)
                        VALUES (?, ?, ?, ?)
                    ''', (device_id, firebase_doc_id, name, shift_timing))
                    
                    print(f"🔗 Mapped: EmployeeId '{emp_id_field}' -> Device ID '{device_id}' (Doc: {firebase_doc_id})")
                    
                except (ValueError, TypeError) as e:
                    print(f"⚠️ Could not parse ID for {emp_id_field}: {e}")
            
            conn.commit()
        print("✅ User sync complete.")
    except Exception as e:
        print(f"❌ Error syncing users: {e}")

def schedule_daily_user_sync():
    """Background worker: Runs the user sync immediately, then every day at 6 PM."""
    while True:
        sync_users_from_firebase()
        
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        
        if now >= target:
            target += timedelta(days=1)
            
        sleep_seconds = (target - now).total_seconds()
        print(f"⏳ Next user sync scheduled in {sleep_seconds/3600:.2f} hours (at 6:00 PM).")
        time.sleep(sleep_seconds)

def save_locally(user_id, timestamp, status, punch_type):
    """Saves raw data to local disk immediately."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO attendance (user_id, timestamp, status, punch_type) VALUES (?, ?, ?, ?)",
                (user_id, str(timestamp), status, punch_type)
            )
        print(f"💾 Buffered: Device User {user_id} at {timestamp}")
    except Exception as e:
        print(f"❌ Database Error: {e}")

def get_business_date(punch_time):
    """
    Business Day Logic: 6:00 PM to 5:59 PM next day.
    If a punch is before 18:00 (6:00 PM), it belongs to the previous day's shift.
    """
    if punch_time.hour < 18:
        return (punch_time - timedelta(days=1)).date()
    return punch_time.date()

def sync_to_firebase():
    """Background worker: Uploads unsynced records to Firestore."""
    print("☁️  Sync Agent Started...")
    while True:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("SELECT * FROM attendance WHERE synced = 0 LIMIT 10")
                rows = cursor.fetchall()

                if not rows:
                    time.sleep(5) 
                    continue

                for row in rows:
                    zk_id = str(row['user_id'])
                    
                    # Look up Firebase ID from local DB
                    cursor.execute("SELECT firebase_id FROM users WHERE device_id = ?", (zk_id,))
                    user_record = cursor.fetchone()
                    
                    if user_record:
                        firebase_user_id = user_record['firebase_id']
                        print(f"✅ Found mapping: Device {zk_id} -> Firebase {firebase_user_id}")
                    else:
                        print(f"⚠️ Warning: Device ID {zk_id} not found in local user cache. Skipping sync.")
                        continue 
                        
                    punch_time = datetime.strptime(row['timestamp'], "%Y-%m-%d %H:%M:%S")
                    
                    # Calculate Global Business Date
                    business_date = get_business_date(punch_time)
                    date_str = business_date.strftime('%Y-%m-%d')
                    doc_id = f"{firebase_user_id}_{date_str}" 
                    
                    is_check_in = (row['status'] == 0)
                    
                    update_data = {
                        "userId": firebase_user_id,
                        "date": datetime.combine(business_date, datetime.min.time()),
                        "lastModifiedBy": "ZKT_BRIDGE",
                        "lastModifiedAt": firestore.SERVER_TIMESTAMP
                    }

                    if is_check_in:
                        update_data["checkInTime"] = punch_time
                        action = "Check-In"
                    else:
                        update_data["checkOutTime"] = punch_time
                        action = "Check-Out"

                    # Upsert to Firestore
                    db.collection('attendance').document(doc_id).set(update_data, merge=True)
                    print(f"🚀 Synced: {firebase_user_id} | {action} | Doc: {doc_id}")

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
            
            for attendance in conn.live_capture():
                if attendance is None:
                    continue
                
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
    init_local_db()
    
    user_sync_thread = threading.Thread(target=schedule_daily_user_sync, daemon=True)
    user_sync_thread.start()
    
    attendance_sync_thread = threading.Thread(target=sync_to_firebase, daemon=True)
    attendance_sync_thread.start()
    
    run_device_listener()
