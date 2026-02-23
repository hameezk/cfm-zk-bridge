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
    print("\n[DEBUG] 🔄 Starting Firebase User Sync...")
    try:
        users_ref = db.collection('users').stream()
        
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM users")
            
            for doc in users_ref:
                data = doc.to_dict()
                firebase_doc_id = doc.id 
                
                emp_id_field = data.get("employeeId") or data.get("employeeID") or data.get("EmployeeId")
                print(f"[DEBUG] Firebase Doc: {firebase_doc_id} | Raw emp_id_field: {repr(emp_id_field)}")
                
                if not emp_id_field:
                    print(f"[DEBUG] -> Skipping: No employeeId found.")
                    continue
                
                try:
                    digits_only = re.sub(r'\D', '', str(emp_id_field))
                    print(f"[DEBUG] -> Cleaned digits_only: {repr(digits_only)}")
                    
                    if not digits_only:
                        continue
                        
                    device_id = str(int(digits_only)) 
                    print(f"[DEBUG] -> Final calculated device_id: {repr(device_id)} (Type: {type(device_id)})")
                    
                    name = data.get('name', 'Unknown')
                    shift_timing = data.get('shiftTiming', '')
                    
                    conn.execute('''
                        INSERT OR REPLACE INTO users (device_id, firebase_id, name, shift_timing)
                        VALUES (?, ?, ?, ?)
                    ''', (device_id, firebase_doc_id, name, shift_timing))
                    
                except Exception as e:
                    print(f"⚠️ [DEBUG] Error parsing ID for {emp_id_field}: {e}")
            
            conn.commit()
            
            # --- DUMP CACHE TO CONSOLE ---
            cursor = conn.cursor()
            cursor.execute("SELECT device_id, firebase_id FROM users")
            all_cached_users = cursor.fetchall()
            print(f"[DEBUG] ✅ Final Local User Cache Contents: {all_cached_users}")
            
    except Exception as e:
        print(f"❌ [DEBUG] Error syncing users: {e}")

def schedule_daily_user_sync():
    while True:
        sync_users_from_firebase()
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        time.sleep(sleep_seconds)

def save_locally(user_id, timestamp, status, punch_type):
    try:
        print(f"\n[DEBUG] --- NEW PUNCH CAPTURED ---")
        print(f"[DEBUG] Raw user_id from hardware: {repr(user_id)} (Type: {type(user_id)})")
        
        clean_user_id = re.sub(r'\D', '', str(user_id))
        print(f"[DEBUG] Cleaned user_id for DB: {repr(clean_user_id)}")
        
        if not clean_user_id:
            return
            
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO attendance (user_id, timestamp, status, punch_type) VALUES (?, ?, ?, ?)",
                (clean_user_id, str(timestamp), status, punch_type)
            )
    except Exception as e:
        print(f"❌ Database Error: {e}")

def get_business_date(punch_time):
    if punch_time.hour < 18:
        return (punch_time - timedelta(days=1)).date()
    return punch_time.date()

def sync_to_firebase():
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
                    print(f"[DEBUG] Raw user_id from attendance table: {repr(raw_db_id)}")
                    
                    zk_id = re.sub(r'\D', '', raw_db_id)
                    print(f"[DEBUG] zk_id configured for lookup: {repr(zk_id)}")
                    
                    # --- DUMP AVAILABLE KEYS ---
                    cursor.execute("SELECT device_id FROM users")
                    available_keys = [r['device_id'] for r in cursor.fetchall()]
                    print(f"[DEBUG] Searching cache... Available device_ids in cache: {available_keys}")
                    
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

                    db.collection('attendance').document(doc_id).set(update_data, merge=True)
                    print(f"🚀 Synced: {firebase_user_id} | {action} | Doc: {doc_id}")

                    conn.execute("UPDATE attendance SET synced = 1 WHERE id = ?", (row['id'],))
                    conn.commit()

        except Exception as e:
            print(f"⚠️ [DEBUG] Sync Agent Error: {e}")
            time.sleep(10)

def run_device_listener():
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