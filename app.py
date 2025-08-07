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
import requests # Ditambahkan untuk mengirim data

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- KONFIGURASI PENTING ---
# Endpoint diubah ke endpoint pengujian lokal di aplikasi ini sendiri.
# Gunakan 127.0.0.1 (localhost) dan port yang sama dengan aplikasi Flask.
CLIP_UPLOAD_ENDPOINT = "http://127.0.0.1:5001/api/test_upload"

# Add custom Jinja2 filters
@app.template_filter('enumerate')
def enumerate_filter(iterable):
    return enumerate(iterable)

# Create directories for media files
os.makedirs('snapshots', exist_ok=True)
os.makedirs('recordings', exist_ok=True)
os.makedirs('clips', exist_ok=True)
os.makedirs('test_uploads', exist_ok=True) # Folder untuk hasil upload pengujian

# Load configuration
try:
    from config import CAMERAS, DEFAULT_USERNAME, DEFAULT_PASSWORD
    cameras = CAMERAS
    default_username = DEFAULT_USERNAME
    default_password = DEFAULT_PASSWORD
except ImportError:
    # Fallback to default configuration
    # Ditambahkan key "lapangan" untuk setiap kamera
    cameras = [
        {"name": "Front Door", "url": "rtsp://admin:ZESFRO@192.168.1.127:554/stream1", "lapangan": 1},
        {"name": "Back Yard", "url": "rtsp://admin:ZESFRO@192.168.1.128:554/stream1", "lapangan": 2},
        {"name": "Garage", "url": "rtsp://admin:ZESFRO@192.168.1.129:110/stream1", "lapangan": 3},
        {"name": "Living Room", "url": "rtsp://admin:ZESFRO@192.168.1.130:554/stream1", "lapangan": 4}
    ]
    default_username = "admin"
    default_password = "admin123"

# Dummy user credentials
user_data = {
    "username": default_username,
    "password": generate_password_hash(default_password)
}

# Global recording state
recording_state = {i: False for i in range(len(cameras))}
recording_threads = {}

# --- FUNGSI BARU UNTUK MENGIRIM KLIP ---
def upload_clip_task(filename, camera_name, lapangan, start_time):
    """Mengirim file video dan metadata ke endpoint API."""
    try:
        if not os.path.exists(filename):
            print(f"[UPLOAD ERROR] File tidak ditemukan: {filename}")
            return

        print(f"[UPLOAD] Mempersiapkan pengiriman file {filename} ke {CLIP_UPLOAD_ENDPOINT}")
        with open(filename, 'rb') as f:
            # 'files' untuk file video, 'data' untuk metadata
            files = {'video': (os.path.basename(filename), f, 'video/avi')}
            payload = {
                'camera_name': camera_name,
                'lapangan': lapangan,
                'start_time': start_time
            }
            
            response = requests.post(CLIP_UPLOAD_ENDPOINT, files=files, data=payload, timeout=60)
            
            if response.status_code == 200 or response.status_code == 201:
                print(f"[UPLOAD SUCCESS] Berhasil mengirim {filename}. Status: {response.status_code}")
            else:
                print(f"[UPLOAD FAILED] Gagal mengirim {filename}. Status: {response.status_code}, Response: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"[UPLOAD FATAL ERROR] Gagal terhubung ke endpoint: {e}")
    except Exception as e:
        print(f"[UPLOAD FATAL ERROR] Terjadi exception di upload_clip_task: {e}")

# --- FUNGSI UNTUK MEMBUAT KLIP (DIPERBARUI) ---
def create_clip_task(camera_id, duration):
    """Fungsi ini merekam klip dan kemudian memicu pengiriman klip tersebut."""
    filename = None
    cap = None
    out = None
    try:
        if camera_id >= len(cameras):
            print(f"[CLIP ERROR] Camera ID {camera_id} tidak ditemukan.")
            return
            
        cam_info = cameras[camera_id]
        start_time_iso = datetime.now().isoformat() # Catat waktu mulai

        print(f"[CLIP] Mencoba membuka stream RTSP untuk '{cam_info['name']}': {cam_info['url']}")
        cap = cv2.VideoCapture(cam_info["url"])
        if not cap.isOpened():
            print(f"[CLIP ERROR] Gagal terhubung ke kamera: {cam_info['name']}.")
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"clips/{cam_info['name'].replace(' ', '_')}_{duration}s_{timestamp}.avi"
        
        fps = int(cap.get(cv2.CAP_PROP_FPS)) or 20
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out = cv2.VideoWriter(filename, fourcc, fps, (width, height))
        
        if not out.isOpened():
            print(f"[CLIP ERROR] Gagal membuat file VideoWriter.")
            return

        print(f"[CLIP STARTED] Mulai merekam klip {duration} detik dari '{cam_info['name']}'")
        
        frame_count = fps * duration
        for i in range(frame_count):
            ret, frame = cap.read()
            if ret:
                out.write(frame)
            else:
                print(f"[CLIP WARNING] Gagal membaca frame pada frame ke-{i+1}.")
                break
        
        print(f"[CLIP FINISHED] Klip berhasil disimpan: {filename}")

    except Exception as e:
        print(f"[CLIP FATAL ERROR] Terjadi exception di create_clip_task: {e}")
    finally:
        if cap and cap.isOpened():
            cap.release()
        if out and out.isOpened():
            out.release()
        print("[CLIP] Semua resource perekaman telah dilepaskan.")

        # --- MULAI PROSES UPLOAD SETELAH REKAMAN SELESAI ---
        if filename and os.path.exists(filename):
            print(f"[UPLOAD] Memulai proses upload untuk file: {filename} di thread terpisah.")
            upload_thread = threading.Thread(
                target=upload_clip_task, 
                args=(filename, cam_info.get('name', 'N/A'), cam_info.get('lapangan', 0), start_time_iso)
            )
            upload_thread.daemon = True
            upload_thread.start()

# --- FUNGSI LISTENER UDP (TETAP SAMA) ---
def udp_listener():
    """Fungsi ini berjalan di thread terpisah untuk mendengarkan pesan UDP dari ESP32."""
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
                
                print(f"➡️  Aksi: Menerima perintah dari ESP32 untuk membuat klip {clip_duration} detik dari kamera ID {camera_to_record}.")
                
                clip_thread = threading.Thread(target=create_clip_task, args=(camera_to_record, clip_duration))
                clip_thread.start()
                
                response = f"ACK: Clip creation started for camera {camera_to_record}"
                sock.sendto(response.encode('utf-8'), addr)

        except Exception as e:
            print(f"[UDP ERROR] Terjadi error di listener UDP: {e}")

class CameraRecorder:
    def __init__(self, camera_id, rtsp_url):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.recording = False
        self.writer = None
        self.thread = None
        
    def start_recording(self):
        if self.recording:
            return False
            
        self.recording = True
        recording_state[self.camera_id] = True
        
        self.thread = threading.Thread(target=self._record_loop)
        self.thread.daemon = True
        self.thread.start()
        return True
        
    def _record_loop(self):
        """Loop rekaman internal dengan error handling."""
        cap = None
        try:
            print(f"[REC] Mencoba membuka stream untuk rekaman: {self.rtsp_url}")
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                print(f"[REC ERROR] Gagal membuka stream untuk kamera ID {self.camera_id}")
                self.recording = False
                recording_state[self.camera_id] = False
                return

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recordings/camera_{self.camera_id}_{timestamp}.avi"
            
            fps = int(cap.get(cv2.CAP_PROP_FPS)) or 20
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            self.writer = cv2.VideoWriter(filename, fourcc, fps, (width, height))

            if not self.writer.isOpened():
                print(f"[REC ERROR] Gagal membuat file VideoWriter untuk kamera ID {self.camera_id}")
                self.recording = False
                recording_state[self.camera_id] = False
                return

            print(f"[REC STARTED] Mulai merekam untuk kamera ID {self.camera_id} ke file {filename}")
            while self.recording:
                ret, frame = cap.read()
                if ret:
                    self.writer.write(frame)
                else:
                    print(f"[REC WARNING] Frame drop pada kamera ID {self.camera_id}. Mencoba lagi...")
                    time.sleep(0.5)
            
        except Exception as e:
            print(f"[REC FATAL ERROR] Exception di thread rekaman kamera {self.camera_id}: {e}")
        finally:
            if cap:
                cap.release()
            if self.writer:
                self.writer.release()
            self.recording = False
            recording_state[self.camera_id] = False
            print(f"[REC STOPPED] Rekaman untuk kamera ID {self.camera_id} berhenti dan resource dilepaskan.")

    def stop_recording(self):
        self.recording = False

# Camera recorder instances
recorders = {i: CameraRecorder(i, cameras[i]["url"]) for i in range(len(cameras))}

def generate_frame(rtsp_url, camera_id):
    cap = cv2.VideoCapture(rtsp_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    
    while True:
        ret, frame = cap.read()
        if not ret:
            frame = create_error_frame()
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, timestamp, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        
        if recording_state.get(camera_id, False):
            cv2.circle(frame, (frame.shape[1] - 30, 30), 10, (0, 0, 255), -1)
            cv2.putText(frame, "REC", (frame.shape[1] - 60, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ret:
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n\r\n')
    
    cap.release()

def create_error_frame():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(frame, "Camera Offline", (200, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    return frame

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ENDPOINT PENGUJIAN BARU ---
@app.route('/api/test_upload', methods=['POST'])
def test_upload():
    """Endpoint ini hanya untuk menerima dan menampilkan data yang dikirim."""
    try:
        print("\n--- [TEST ENDPOINT RECEIVED] ---")
        
        # Ambil dan cetak data dari form
        camera_name = request.form.get('camera_name')
        lapangan = request.form.get('lapangan')
        start_time = request.form.get('start_time')
        print(f"  Camera Name: {camera_name}")
        print(f"  Lapangan: {lapangan}")
        print(f"  Start Time: {start_time}")

        # Ambil, cetak, dan simpan file video
        video_file = request.files.get('video')
        if video_file:
            filename = os.path.join('test_uploads', video_file.filename)
            video_file.save(filename)
            print(f"  Video File: '{video_file.filename}' berhasil disimpan ke '{filename}'")
        else:
            print("  Video File: Tidak ditemukan dalam request.")

        print("--- [END OF TEST ENDPOINT] ---\n")
        return jsonify({"status": "success", "message": "Data diterima dengan baik untuk pengujian"}), 200

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
    return render_template('index.html', 
                         cameras=cameras, 
                         cameras_with_index=cameras_with_index,
                         recording_state=recording_state)

@app.route('/fullscreen/<int:camera_id>')
@login_required
def fullscreen(camera_id):
    if camera_id >= len(cameras):
        return redirect(url_for('index'))
    return render_template('fullscreen.html', camera=cameras[camera_id], camera_id=camera_id)

@app.route('/video_feed/<int:camera_id>')
def video_feed(camera_id):
    if camera_id >= len(cameras):
        return "Camera not found", 404
    return Response(generate_frame(cameras[camera_id]["url"], camera_id),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

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

@app.route('/start_recording/<int:camera_id>')
@login_required
def start_recording(camera_id):
    if camera_id >= len(cameras):
        return jsonify({"error": "Camera not found"}), 404
    if recorders[camera_id].start_recording():
        return jsonify({"message": f"Recording started for {cameras[camera_id]['name']}"})
    else:
        return jsonify({"error": "Failed to start recording"}), 500

@app.route('/stop_recording/<int:camera_id>')
@login_required
def stop_recording(camera_id):
    if camera_id >= len(cameras):
        return jsonify({"error": "Camera not found"}), 404
    recorders[camera_id].stop_recording()
    return jsonify({"message": f"Recording stopped for {cameras[camera_id]['name']}"})

@app.route('/start_all_recording')
@login_required
def start_all_recording():
    results = []
    for i, camera in enumerate(cameras):
        if recorders[i].start_recording():
            results.append({"camera": camera["name"], "status": "started"})
        else:
            results.append({"camera": camera["name"], "status": "failed"})
    return jsonify({"message": "Batch recording started", "results": results})

@app.route('/stop_all_recording')
@login_required
def stop_all_recording():
    for i in range(len(cameras)):
        recorders[i].stop_recording()
    return jsonify({"message": "All recordings stopped"})

@app.route('/clip/<int:camera_id>/<int:duration>')
@login_required
def clip(camera_id, duration=15):
    """Memulai pembuatan klip di latar belakang dan langsung merespons."""
    print(f"[HTTP] Memicu pembuatan klip {duration} detik dari kamera ID {camera_id} via web.")
    
    clip_thread = threading.Thread(target=create_clip_task, args=(camera_id, duration))
    clip_thread.daemon = True
    clip_thread.start()
    
    return jsonify({"message": f"Clip creation for {duration}s has been started in the background."})

@app.route('/api/recording_status')
@login_required
def recording_status():
    return jsonify(recording_state)

@app.route('/api/camera_status')
@login_required
def camera_status():
    status = {}
    for i, camera in enumerate(cameras):
        cap = cv2.VideoCapture(camera["url"])
        status[i] = {
            "name": camera["name"],
            "online": cap.isOpened(),
            "recording": recording_state.get(i, False)
        }
        cap.release()
    return jsonify(status)

if __name__ == '__main__':
    listener_thread = threading.Thread(target=udp_listener, daemon=True)
    listener_thread.start()

    print("🎥 RTSP Camera Surveillance System")
    print("=" * 50)
    print("📋 Features:")
    print("   • Multi-camera RTSP streaming")
    print("   • Real-time recording & snapshots")
    print("   • Fullscreen camera view")
    print("   • Video clip generation")
    print("   • Modern responsive UI")
    print("   • Secure authentication")
    print("   • UDP Trigger for clip recording")
    print("   • Auto-upload clips to an endpoint")
    print()
    print("🔐 Default Login:")
    print("   Username: admin")
    print("   Password: admin123")
    print()
    print("🌐 Starting server...")
    print("   Access at: http://localhost:5001")
    print("=" * 50)
    
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True)
