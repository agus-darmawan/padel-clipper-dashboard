import os
import cv2
import threading
import time
import json
import numpy as np
from datetime import datetime
from flask import Flask, render_template, Response, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import socket
import requests
from collections import deque # Diperlukan untuk buffer yang efisien

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- KONFIGURASI PENTING (DIOPTIMALKAN) ---
CLIP_UPLOAD_ENDPOINT = "http://127.0.0.1:5001/api/test_upload"
# Buffer dikurangi menjadi 15 detik untuk menghemat RAM dan mencegah hang.
MAX_PRE_EVENT_SECONDS = 30
CCTV_CHUNK_MINUTES = 30 # Durasi setiap file rekaman CCTV berkelanjutan

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
        self.buffer = deque(maxlen=1) # Buffer awal kecil
        self.buffer_lock = threading.Lock()
        self.last_frame = create_error_frame()
        self.is_online = False
        self.fps = 20 # Nilai default
        
        # HANYA SATU THREAD PER KAMERA untuk menangkap video
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()

    def _capture_loop(self):
        """Loop tunggal ini menangani SEMUANYA: buffering, rekaman CCTV, dan live frame."""
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
                    # Inisialisasi buffer dengan ukuran yang benar
                    with self.buffer_lock:
                        self.buffer = deque(maxlen=buffer_size)
                    print(f"[CAPTURE] Kamera ID {self.camera_id} terhubung. FPS: {self.fps}, Buffer: {buffer_size} frames.")

                # 2. BACA FRAME
                ret, frame = cap.read()
                if not ret:
                    print(f"[CAPTURE WARNING] Gagal membaca frame dari kamera ID {self.camera_id}. Menghubungkan ulang...")
                    cap.release()
                    cap = None
                    self.is_online = False
                    continue

                # 3. DISTRIBUSI FRAME
                self.last_frame = frame # Untuk live view
                with self.buffer_lock:
                    self.buffer.append(frame) # Untuk klip pre-event

                # 4. LOGIKA REKAMAN CCTV
                # Jika writer belum ada atau sudah waktunya membuat file baru
                if cctv_writer is None or time.time() > cctv_end_time:
                    if cctv_writer is not None:
                        cctv_writer.release()
                        print(f"[CCTV FINISHED] Segmen rekaman sebelumnya untuk kamera ID {self.camera_id} selesai.")
                    
                    height, width, _ = frame.shape
                    fourcc = cv2.VideoWriter_fourcc(*'XVID')
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"recordings/{self.cam_info['name'].replace(' ', '_')}_{timestamp}.avi"
                    cctv_writer = cv2.VideoWriter(filename, fourcc, self.fps, (width, height))
                    cctv_end_time = time.time() + CCTV_CHUNK_MINUTES * 60
                    print(f"[CCTV STARTED] Mulai merekam segmen baru untuk kamera ID {self.camera_id} ke file {filename}")

                # Tulis frame ke file CCTV
                cctv_writer.write(frame)

            except Exception as e:
                print(f"[CAPTURE FATAL ERROR] Exception di loop utama kamera {self.camera_id}: {e}")
                time.sleep(10)

    def create_pre_event_clip(self, requested_duration):
        """Fungsi ini mengambil N detik terakhir dari buffer memori."""
        if not self.is_online or not self.buffer:
            print(f"[CLIP ERROR] Tidak bisa membuat klip, kamera ID {self.camera_id} sedang offline atau buffer kosong.")
            return

        # Batasi durasi klip agar tidak melebihi kapasitas buffer
        if requested_duration > MAX_PRE_EVENT_SECONDS:
            print(f"[CLIP WARNING] Durasi {requested_duration}s melebihi buffer. Dibatasi menjadi {MAX_PRE_EVENT_SECONDS}s.")
            requested_duration = MAX_PRE_EVENT_SECONDS

        print(f"[CLIP] Memulai pembuatan klip {requested_duration}s (Hanya Pre-Event) untuk kamera ID {self.camera_id}")
        
        all_frames = []
        with self.buffer_lock:
            frames_to_take = self.fps * requested_duration
            all_frames = list(self.buffer)[-frames_to_take:]

        if not all_frames:
            print("[CLIP ERROR] Tidak ada frame yang bisa diproses setelah mengambil dari buffer.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"clips/{self.cam_info['name'].replace(' ', '_')}_{requested_duration}s_{timestamp}.avi"
        
        height, width, _ = all_frames[0].shape
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out = cv2.VideoWriter(filename, fourcc, self.fps, (width, height))
        
        for frame in all_frames:
            out.write(frame)
        
        out.release()
        print(f"[CLIP FINISHED] Klip pre-event berhasil disimpan: {filename}")
        
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
    """Mengirim file video dan metadata ke endpoint API."""
    try:
        if not os.path.exists(filename):
            print(f"[UPLOAD ERROR] File tidak ditemukan: {filename}")
            return

        print(f"[UPLOAD] Mempersiapkan pengiriman file {filename} ke {CLIP_UPLOAD_ENDPOINT}")
        with open(filename, 'rb') as f:
            files = {'video': (os.path.basename(filename), f, 'video/avi')}
            payload = {
                'camera_name': camera_name,
                'lapangan': lapangan,
                'start_time': start_time
            }
            
            response = requests.post(CLIP_UPLOAD_ENDPOINT, files=files, data=payload, timeout=60)
            
            if response.status_code in [200, 201]:
                print(f"[UPLOAD SUCCESS] Berhasil mengirim {filename}. Status: {response.status_code}")
            else:
                print(f"[UPLOAD FAILED] Gagal mengirim {filename}. Status: {response.status_code}, Response: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"[UPLOAD FATAL ERROR] Gagal terhubung ke endpoint: {e}")
    except Exception as e:
        print(f"[UPLOAD FATAL ERROR] Terjadi exception di upload_clip_task: {e}")

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

@app.route('/api/test_upload', methods=['POST'])
def test_upload():
    try:
        print("\n--- [TEST ENDPOINT RECEIVED] ---")
        camera_name = request.form.get('camera_name')
        lapangan = request.form.get('lapangan')
        start_time = request.form.get('start_time')
        print(f"  Camera Name: {camera_name}")
        print(f"  Lapangan: {lapangan}")
        print(f"  Start Time: {start_time}")
        video_file = request.files.get('video')
        if video_file:
            filename = os.path.join('test_uploads', video_file.filename)
            video_file.save(filename)
            print(f"  Video File: '{video_file.filename}' berhasil disimpan ke '{filename}'")
        else:
            print("  Video File: Tidak ditemukan dalam request.")
        print("--- [END OF TEST ENDPOINT] ---\n")
        return jsonify({"status": "success", "message": "Data diterima"}), 200
    except Exception as e:
        print(f"[TEST ENDPOINT ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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
    # PERBAIKAN: Buat status rekaman on-the-fly karena rekaman CCTV selalu aktif
    # Ini akan mencegah error 'Undefined' di template Jinja2.
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
