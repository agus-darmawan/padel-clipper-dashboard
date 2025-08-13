import os
import cv2
import threading
import time
import json
import numpy as np
from datetime import datetime, timedelta
from flask import Flask, render_template, Response, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import socket
import requests
from collections import deque

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- KONFIGURASI PENTING  ---
API_BASE_URL = "http://103.175.219.138:5009/api/v1"
CLIP_UPLOAD_ENDPOINT = f"{API_BASE_URL}/clips/upload"
COURTS_ENDPOINT = f"{API_BASE_URL}/courts"
BOOKING_HOURS_ENDPOINT = f"{API_BASE_URL}/booking-hours"

# Buffer dikurangi menjadi 15 detik untuk menghemat RAM dan mencegah hang.
MAX_PRE_EVENT_SECONDS = 15
CCTV_CHUNK_MINUTES = 10 # Durasi setiap file rekaman CCTV berkelanjutan

ALLOWED_VIDEO_TYPES = ['video/mp4', 'video/webm', 'video/ogg']

# --- DIREKTORI & KONFIGURASI KAMERA ---
os.makedirs('snapshots', exist_ok=True)
os.makedirs('clips', exist_ok=True)
os.makedirs('recordings', exist_ok=True)
os.makedirs('test_uploads', exist_ok=True)

try:
    from config import CAMERAS, DEFAULT_USERNAME, DEFAULT_PASSWORD
    cameras = CAMERAS
    default_username = DEFAULT_USERNAME
    default_password = DEFAULT_PASSWORD
except ImportError:
    cameras = [
        {"name": "Front Door", "url": "rtsp://admin:ZESFRO@192.168.1.127:554/stream1", "lapangan": 1},
        {"name": "Back Yard", "url": "rtsp://admin:ZESFRO@192.168.1.128:554/stream1", "lapangan": 2},
        {"name": "Garage", "url": "rtsp://admin:ZESFRO@192.168.1.129:110/stream1", "lapangan": 3},
        {"name": "Living Room", "url": "rtsp://admin:ZESFRO@192.168.1.130:554/stream1", "lapangan": 4}
    ]
    default_username = "admin"
    default_password = "admin123"

# --- KREDENSIAL & STATUS ---
user_data = {"username": default_username, "password": generate_password_hash(default_password)}

# --- FUNGSI BANTU ---
def create_error_frame():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(frame, "Camera Offline", (200, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    return frame

# --- KELAS KAMERA YANG DIOPTIMALKAN ---
class AdvancedCamera:
    def __init__(self, camera_id, cam_info):
        self.camera_id = camera_id
        self.cam_info = cam_info
        self.rtsp_url = cam_info['url']
        self.is_running = True
        self.buffer = deque(maxlen=1)
        self.buffer_lock = threading.Lock()
        self.last_frame = create_error_frame()
        self.is_online = False
        self.fps = 20
        
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()

    def _capture_loop(self):
        """Loop yang diperbarui untuk mendukung MP4"""
        cap = None
        cctv_writer = None
        cctv_end_time = 0

        while self.is_running:
            try:
                # 1. KONEKSI & INISIALISASI
                if cap is None or not cap.isOpened():
                    print(f"[CAPTURE] Mencoba menghubungkan ke kamera ID {self.camera_id}...")
                    cap = cv2.VideoCapture(self.rtsp_url)
                    if not cap.isOpened():
                        self.is_online = False
                        print(f"[CAPTURE ERROR] Gagal terhubung ke kamera ID {self.camera_id}. Mencoba lagi...")
                        time.sleep(10)
                        continue
                    
                    self.is_online = True
                    self.fps = int(cap.get(cv2.CAP_PROP_FPS)) or 20
                    buffer_size = self.fps * MAX_PRE_EVENT_SECONDS
                    with self.buffer_lock:
                        self.buffer = deque(maxlen=buffer_size)
                    print(f"[CAPTURE] Kamera ID {self.camera_id} terhubung. FPS: {self.fps}, Buffer: {buffer_size} frames.")

                ret, frame = cap.read()
                if not ret:
                    print(f"[CAPTURE WARNING] Gagal membaca frame dari kamera ID {self.camera_id}. Menghubungkan ulang...")
                    cap.release()
                    cap = None
                    self.is_online = False
                    continue

                self.last_frame = frame
                with self.buffer_lock:
                    self.buffer.append(frame)

                # 4. LOGIKA REKAMAN CCTV - GUNAKAN MP4 LANGSUNG
                if cctv_writer is None or time.time() > cctv_end_time:
                    if cctv_writer is not None:
                        cctv_writer.release()
                        print(f"[CCTV FINISHED] Segmen rekaman sebelumnya untuk kamera ID {self.camera_id} selesai.")
                    
                    height, width, _ = frame.shape
                    # PERBAIKAN: Gunakan codec MP4 langsung
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # Atau gunakan 'avc1' untuk H.264
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    # PERBAIKAN: Ganti ekstensi ke .mp4
                    filename = f"recordings/{self.cam_info['name'].replace(' ', '_')}_{timestamp}.mp4"
                    cctv_writer = cv2.VideoWriter(filename, fourcc, self.fps, (width, height))
                    cctv_end_time = time.time() + CCTV_CHUNK_MINUTES * 60
                    print(f"[CCTV STARTED] Mulai merekam segmen MP4 baru untuk kamera ID {self.camera_id} ke file {filename}")

                cctv_writer.write(frame)

            except Exception as e:
                print(f"[CAPTURE FATAL ERROR] Exception di loop utama kamera {self.camera_id}: {e}")
                time.sleep(10)

    def create_pre_event_clip(self, requested_duration):
        """Fungsi yang diperbarui untuk membuat klip MP4"""
        if not self.is_online or not self.buffer:
            print(f"[CLIP ERROR] Tidak bisa membuat klip, kamera ID {self.camera_id} sedang offline atau buffer kosong.")
            return

        if requested_duration > MAX_PRE_EVENT_SECONDS:
            print(f"[CLIP WARNING] Durasi {requested_duration}s melebihi buffer. Dibatasi menjadi {MAX_PRE_EVENT_SECONDS}s.")
            requested_duration = MAX_PRE_EVENT_SECONDS

        print(f"[CLIP] Memulai pembuatan klip MP4 {requested_duration}s (Pre-Event) untuk kamera ID {self.camera_id}")
        
        all_frames = []
        with self.buffer_lock:
            frames_to_take = self.fps * requested_duration
            all_frames = list(self.buffer)[-frames_to_take:]

        if not all_frames:
            print("[CLIP ERROR] Tidak ada frame yang bisa diproses setelah mengambil dari buffer.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # PERBAIKAN: Langsung buat file MP4
        filename = f"clips/{self.cam_info['name'].replace(' ', '_')}_{requested_duration}s_{timestamp}.mp4"
        
        height, width, _ = all_frames[0].shape
        # PERBAIKAN: Gunakan codec MP4
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # Atau 'avc1' untuk H.264
        out = cv2.VideoWriter(filename, fourcc, self.fps, (width, height))
        
        if not out.isOpened():
            print(f"[CLIP ERROR] Gagal membuka video writer untuk {filename}")
            # Fallback ke codec lain jika mp4v gagal
            print("[CLIP] Mencoba codec alternatif...")
            fourcc = cv2.VideoWriter_fourcc(*'avc1')  # H.264
            out = cv2.VideoWriter(filename, fourcc, self.fps, (width, height))
            
            if not out.isOpened():
                print("[CLIP ERROR] Semua codec gagal. Menggunakan fallback AVI.")
                filename = filename.replace('.mp4', '.avi')
                fourcc = cv2.VideoWriter_fourcc(*'XVID')
                out = cv2.VideoWriter(filename, fourcc, self.fps, (width, height))
        
        for frame in all_frames:
            out.write(frame)
        
        out.release()
        print(f"[CLIP FINISHED] Klip berhasil disimpan: {filename}")
        
        upload_thread = threading.Thread(
            target=upload_clip_task, 
            args=(filename, self.cam_info.get('name', 'N/A'), self.cam_info.get('lapangan', 0), datetime.now().isoformat())
        )
        upload_thread.daemon = True
        upload_thread.start()

# Inisialisasi semua kamera
advanced_cameras = {i: AdvancedCamera(i, cam_info) for i, cam_info in enumerate(cameras)}

# --- FUNGSI UPLOAD & LISTENER UDP ---
def upload_clip_task(filename, camera_name, lapangan, start_time):
    """Mengirim file video dan metadata ke endpoint API - FIXED VERSION."""
    try:
        if not os.path.exists(filename):
            print(f"[UPLOAD ERROR] File tidak ditemukan: {filename}")
            return

        print(f"[UPLOAD] Mempersiapkan pengiriman file {filename} ke {CLIP_UPLOAD_ENDPOINT}")
        
        # Test endpoint connectivity first
        try:
            test_response = requests.get(f"{API_BASE_URL}/health", timeout=5)
            print(f"[UPLOAD DEBUG] API health check: {test_response.status_code}")
        except:
            print(f"[UPLOAD WARNING] Cannot reach API health endpoint")
        
        # 1. Dapatkan atau buat court
        print(f"[UPLOAD] Getting court for camera: {camera_name}")
        try:
            court_response = requests.get(f"{COURTS_ENDPOINT}?name={camera_name}", timeout=10)
            print(f"[UPLOAD DEBUG] Court GET response: {court_response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[UPLOAD ERROR] Failed to get court: {e}")
            return
            
        if court_response.status_code == 404:
            # Buat court baru
            print(f"[UPLOAD] Creating new court: {camera_name}")
            court_create_response = requests.post(
                COURTS_ENDPOINT, 
                json={"name": camera_name},
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if court_create_response.status_code not in [200, 201]:
                print(f"[UPLOAD ERROR] Gagal membuat court: {court_create_response.status_code} - {court_create_response.text}")
                return
            court_data = court_create_response.json()
        else:
            court_data = court_response.json()
        
        # Extract court ID properly
        if 'data' in court_data:
            if isinstance(court_data['data'], list) and len(court_data['data']) > 0:
                court = court_data['data'][0]
            else:
                court = court_data['data']
        else:
            court = court_data
            
        court_id = court.get('id')
        if not court_id:
            print(f"[UPLOAD ERROR] No court ID found in response: {court_data}")
            return
            
        print(f"[UPLOAD INFO] Using court ID: {court_id}")
        
        # 2. Buat booking hour untuk clip ini
        print(f"[UPLOAD] Creating booking hour for court {court_id}")
        booking_payload = {
            "courtId": int(court_id),  # Ensure it's an integer
            "dateStart": start_time,
            "dateEnd": (datetime.fromisoformat(start_time.replace('Z', '+00:00') if start_time.endswith('Z') else start_time) + timedelta(minutes=15)).isoformat()
        }
        
        try:
            booking_response = requests.post(
                BOOKING_HOURS_ENDPOINT, 
                json=booking_payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            print(f"[UPLOAD DEBUG] Booking response: {booking_response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[UPLOAD ERROR] Failed to create booking: {e}")
            return
            
        if booking_response.status_code not in [200, 201]:
            print(f"[UPLOAD ERROR] Gagal membuat booking hour: {booking_response.status_code} - {booking_response.text}")
            return
        
        booking_data = booking_response.json()
        booking_hour_id = booking_data.get('data', {}).get('id')
        
        if not booking_hour_id:
            print(f"[UPLOAD ERROR] No booking hour ID in response: {booking_data}")
            return
            
        print(f"[UPLOAD INFO] Booking hour created with ID: {booking_hour_id}")

        # 3. Upload video clip - Check file type first
        file_size = os.path.getsize(filename)
        print(f"[UPLOAD INFO] File size: {file_size} bytes")
        
        # Check if file is too large (adjust limit as needed)
        if file_size > 100 * 1024 * 1024:  # 100MB limit
            print(f"[UPLOAD ERROR] File too large: {file_size} bytes")
            return
            
        with open(filename, 'rb') as f:
            clip_name = os.path.basename(filename)
            
            # Try different MIME types based on file extension
            file_ext = os.path.splitext(filename)[1].lower()
            if file_ext == '.avi':
                mime_type = 'video/x-msvideo'
            elif file_ext == '.mp4':
                mime_type = 'video/mp4'
            elif file_ext == '.webm':
                mime_type = 'video/webm'
            else:
                mime_type = 'video/avi'  # fallback
            
            files = {'video': (clip_name, f, mime_type)}
            
            # Send only bookingHourId as required by your TypeScript controller
            payload = {
                'bookingHourId': str(booking_hour_id)
            }
            
            print(f"[UPLOAD DEBUG] Endpoint: {CLIP_UPLOAD_ENDPOINT}")
            print(f"[UPLOAD DEBUG] Payload: {payload}")
            print(f"[UPLOAD DEBUG] File: {clip_name} ({mime_type})")
            
            try:
                response = requests.post(
                    CLIP_UPLOAD_ENDPOINT, 
                    files=files, 
                    data=payload, 
                    timeout=120
                )
                
                print(f"[UPLOAD DEBUG] Upload response status: {response.status_code}")
                print(f"[UPLOAD DEBUG] Upload response headers: {dict(response.headers)}")
                
                if response.status_code in [200, 201]:
                    print(f"[UPLOAD SUCCESS] Successfully uploaded {filename}")
                    print(f"[UPLOAD SUCCESS] Response: {response.text}")
                    
                    # Optionally delete local file after successful upload
                    # os.remove(filename)
                    # print(f"[CLEANUP] Local file {filename} deleted")
                    
                else:
                    print(f"[UPLOAD FAILED] Failed to upload {filename}")
                    print(f"[UPLOAD FAILED] Status: {response.status_code}")
                    print(f"[UPLOAD FAILED] Response: {response.text}")
                    
                    # Save response for debugging
                    with open(f"debug_response_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt", 'w') as debug_file:
                        debug_file.write(f"Status: {response.status_code}\n")
                        debug_file.write(f"Headers: {dict(response.headers)}\n")
                        debug_file.write(f"Response: {response.text}\n")
                        debug_file.write(f"Request URL: {CLIP_UPLOAD_ENDPOINT}\n")
                        debug_file.write(f"Request payload: {payload}\n")
                        
            except requests.exceptions.Timeout:
                print(f"[UPLOAD ERROR] Timeout while uploading {filename}")
            except requests.exceptions.ConnectionError:
                print(f"[UPLOAD ERROR] Cannot connect to API server")
            except requests.exceptions.RequestException as e:
                print(f"[UPLOAD ERROR] Request exception: {e}")

    except Exception as e:
        print(f"[UPLOAD FATAL ERROR] Exception in upload_clip_task: {e}")
        import traceback
        traceback.print_exc()

def udp_listener():
    UDP_IP = "0.0.0.0"
    UDP_PORT = 12345
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    print(f"✅ Server UDP aktif, mendengarkan di port {UDP_PORT}...")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            message = data.decode('utf-8')
            print(f"\n[UDP RECEIVED] Pesan diterima dari {addr}: {message}")
            if message == "CREATE_CLIP":
                camera_to_record = 0 
                clip_duration = 15
                print(f"➡️  Aksi: Memicu klip pre-event {clip_duration} detik dari kamera ID {camera_to_record}.")
                clip_thread = threading.Thread(target=advanced_cameras[camera_to_record].create_pre_event_clip, args=(clip_duration,))
                clip_thread.start()
                response = f"ACK: Pre-event clip started for camera {camera_to_record}"
                sock.sendto(response.encode('utf-8'), addr)
        except Exception as e:
            print(f"[UDP ERROR] Terjadi error di listener UDP: {e}")

# --- RUTE-RUTE FLASK ---
def generate_frame(camera_id):
    """Mengambil frame terakhir dari buffer, bukan membuka koneksi baru."""
    while True:
        frame = advanced_cameras[camera_id].last_frame
        ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ret:
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n\r\n')
        time.sleep(1/30)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.template_filter('enumerate')
def enumerate_filter(iterable):
    return enumerate(iterable)

# Endpoint dummy /api/test_upload di-nonaktifkan karena upload diarahkan ke backend Go

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == user_data["username"] and check_password_hash(user_data["password"], password):
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not check_password_hash(user_data["password"], current_password):
            return render_template('change_password.html', error="Current password is incorrect")
        
        if new_password != confirm_password:
            return render_template('change_password.html', error="New passwords do not match")
        
        if len(new_password) < 6:
            return render_template('change_password.html', error="Password must be at least 6 characters long")
        
        user_data["password"] = generate_password_hash(new_password)
        return redirect(url_for('index'))
    
    return render_template('change_password.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    cameras_with_index = list(enumerate(cameras))
    current_recording_state = {i: True for i in range(len(cameras))}
    return render_template('index.html', 
                         cameras=cameras, 
                         cameras_with_index=cameras_with_index,
                         recording_state=current_recording_state)

@app.route('/video_feed/<int:camera_id>')
def video_feed(camera_id):
    if camera_id not in advanced_cameras:
        return "Camera not found", 404
    return Response(generate_frame(camera_id),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/clip/<int:camera_id>/<int:duration>')
@login_required
def clip(camera_id, duration=15):
    if camera_id not in advanced_cameras:
        return jsonify({"error": "Camera not found"}), 404
    
    print(f"[HTTP] Memicu klip pre-event {duration} detik dari kamera ID {camera_id} via web.")
    clip_thread = threading.Thread(target=advanced_cameras[camera_id].create_pre_event_clip, args=(duration,))
    clip_thread.daemon = True
    clip_thread.start()
    
    return jsonify({"message": f"Pre-event clip creation for {duration}s has been started."})

@app.route('/snapshot/<int:camera_id>')
@login_required
def snapshot(camera_id):
    if camera_id >= len(cameras):
        return jsonify({"error": "Camera not found"}), 404
    cap = cv2.VideoCapture(cameras[camera_id]["url"])
    ret, frame = cap.read()
    if ret:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"snapshots/{cameras[camera_id]['name'].replace(' ', '_')}_{timestamp}.jpg"
        cv2.imwrite(filename, frame)
        cap.release()
        return jsonify({"message": f"Snapshot saved successfully", "filename": filename})
    cap.release()
    return jsonify({"error": "Failed to take snapshot"}), 500

@app.route('/snapshot_all')
@login_required
def snapshot_all():
    results = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for i, camera in enumerate(cameras):
        cap = cv2.VideoCapture(camera["url"])
        ret, frame = cap.read()
        if ret:
            filename = f"snapshots/{camera['name'].replace(' ', '_')}_{timestamp}.jpg"
            cv2.imwrite(filename, frame)
            results.append({"camera": camera["name"], "status": "success", "filename": filename})
        else:
            results.append({"camera": camera["name"], "status": "failed"})
        cap.release()
    return jsonify({"message": "Batch snapshot completed", "results": results})
if __name__ == '__main__':
    listener_thread = threading.Thread(target=udp_listener, daemon=True)
    listener_thread.start()
    
    print("🎥 Advanced RTSP Camera Surveillance System")
    print("=" * 50)
    print("📋 Features:")
    print("   • Pre-event clip recording (on-demand)")
    print("   • Continuous CCTV recording (30-min chunks)")
    print("   • Auto-upload clips to an endpoint")
    print("   • Secure authentication & Live View")
    print()
    print("🔐 Default Login:")
    print(f"   Username: {default_username}")
    print(f"   Password: {default_password}")
    print()
    print("🌐 Starting server...")
    print("   Access at: http://127.0.0.1:5001")
    print("=" * 50)
    
    # use_reloader=False penting untuk mencegah thread berjalan dua kali dalam mode debug
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True, use_reloader=False)
