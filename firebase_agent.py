import time
import sqlite3
import threading
import firebase_admin
import re
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
    """Fetches users from Firestore, extracts IDs safely, and caches them locally."""
    print("\n[DEBUG] 🔄 Starting Firebase User Sync...")
    try:
        # 1. Fetch data from Firebase FIRST before touching the local database
        users_docs = list(db.collection('users').stream())
        
        if not users_docs:
            print("⚠️ [DEBUG] No users found in Firebase or internet is down. Keeping existing local cache.")
            return

        with sqlite3.connect(DB_PATH) as conn:
            # 2. Only clear the cache AFTER we successfully have the Firebase data
            conn.execute("DELETE FROM users")
            
            for doc in users_docs:
                data = doc.to_dict()
                firebase_doc_id = doc.id 
                
                emp_id_field = data.get("employeeId") or data.get("employeeID") or data.get("EmployeeId")
                
                if not emp_id_field:
                    continue
                
                try:
                    # Strip all non-numeric characters using regex
                    digits_only = re.sub(r'\D', '', str(emp_id_field))
                    if not digits_only:
                        continue
                        
                    # Convert to int to drop leading zeros, then back to string
                    device_id = str(int(digits_only)) 
                    name = data.get('name', 'Unknown')
                    shift_timing = data.get('shiftTiming', '')
                    
                    conn.execute('''
                        INSERT OR REPLACE INTO users (device_id, firebase_id, name, shift_timing)
                        VALUES (?, ?, ?, ?)
                    ''', (device_id, firebase_doc_id, name, shift_timing))
                    
                except Exception as e:
                    print(f"⚠️ [DEBUG] Error parsing ID for {emp_id_field}: {e}")
            
            conn.commit()
            
            # --- DUMP CACHE TO CONSOLE TO VERIFY ---
            cursor = conn.cursor()
            cursor.execute("SELECT device_id FROM users")
            available_keys = [r[0] for r in cursor.fetchall()]
            print(f"[DEBUG] ✅ User Sync Complete. Cache currently holds {len(available_keys)} users: {available_keys}")
            
    except Exception as e:
        print(f"❌ [DEBUG] Error syncing users: {e}")

def schedule_daily_user_sync():
    """Background worker: Calculates time until 6 PM, sleeps, then syncs."""
    while True:
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        
        if now >= target:
            target += timedelta(days=1)
            
        sleep_seconds = (target - now).total_seconds()
        print(f"⏳ Next background user sync scheduled in {sleep_seconds/3600:.2f} hours (at 6:00 PM).")
        time.sleep(sleep_seconds)
        
        sync_users_from_firebase()

def save_locally(user_id, timestamp, status, punch_type):
    """Saves raw data to local disk immediately."""
    try:
        # Strip all non-digits from the machine's ID before saving it to the database
        clean_user_id = re.sub(r'\D', '', str(user_id))
        
        if not clean_user_id:
            print(f"⚠️ Warning: Captured blank or invalid user ID from device: '{user_id}'")
            return
            
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO attendance (user_id, timestamp, status, punch_type) VALUES (?, ?, ?, ?)",
                (clean_user_id, str(timestamp), status, punch_type)
            )
        print(f"💾 Buffered: Device User '{clean_user_id}' at {timestamp}")
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
                    print(f"\n[DEBUG] --- PROCESSING ATTENDANCE ROW ID: {row['id']} ---")
                    raw_db_id = str(row['user_id'])
                    
                    # Clean the ID pulled from historical records in the local DB
                    zk_id = re.sub(r'\D', '', raw_db_id)
                    print(f"[DEBUG] zk_id configured for lookup: {repr(zk_id)}")
                    
                    if not zk_id:
                        print(f"⚠️ Corrupted record found (ID: {row['id']}). Marking as synced to skip.")
                        conn.execute("UPDATE attendance SET synced = 1 WHERE id = ?", (row['id'],))
                        conn.commit()
                        continue
                    
                    # Look up Firebase ID from local DB
                    cursor.execute("SELECT firebase_id FROM users WHERE device_id = ?", (zk_id,))
                    user_record = cursor.fetchone()
                    
                    if user_record:
                        firebase_user_id = user_record['firebase_id']
                        print(f"[DEBUG] ✅ MATCH FOUND! device_id {repr(zk_id)} == firebase_id {repr(firebase_user_id)}")
                    else:
                        print(f"⚠️ Warning: Device ID {repr(zk_id)} not found in local user cache. Skipping sync.")
                        continue 
                        
                    punch_time = datetime.strptime(row['timestamp'], "%Y-%m-%d %H:%M:%S")
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
                    print(f"🚀 Synced: Firebase ID '{firebase_user_id}' | {action} | Doc: {doc_id}")

                    # Mark as synced
                    conn.execute("UPDATE attendance SET synced = 1 WHERE id = ?", (row['id'],))
                    conn.commit()

        except Exception as e:
            print(f"⚠️ [DEBUG] Sync Agent Error: {e}")
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
    
    # 💥 BOOT FIX: Force the script to fetch users BEFORE starting the attendance threads
    print("🚀 Booting up: Running initial user sync to populate cache...")
    sync_users_from_firebase() 
    
    # Now that the cache is safely populated, start the background workers
    user_sync_thread = threading.Thread(target=schedule_daily_user_sync, daemon=True)
    user_sync_thread.start()
    
    attendance_sync_thread = threading.Thread(target=sync_to_firebase, daemon=True)
    attendance_sync_thread.start()
    
    # Start listening to the hardware on the main thread
    run_device_listener()