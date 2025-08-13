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
import subprocess

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
os.makedirs('temp_clips', exist_ok=True)  # For temporary AVI files before conversion

try:
    from config import CAMERAS, DEFAULT_USERNAME, DEFAULT_PASSWORD
    cameras = CAMERAS
    default_username = DEFAULT_USERNAME
    default_password = DEFAULT_PASSWORD
except ImportError:
    cameras = [
        {"name": "Lapangan 1 Kiri", "url": "rtsp://admin:ZESFRO@192.168.1.127:554/stream1", "lapangan": 1},
        {"name": "Lapangan 1 Kanan", "url": "rtsp://admin:Josephwijaya34@192.168.1.154:554/stream1", "lapangan": 1},
        {"name": "Lapangan 2 Kiri", "url": "rtsp://admin:Josephwijaya34@192.168.1.155:554/stream1", "lapangan": 2},
        {"name": "Lapangan 2 Kanan", "url": "rtsp://admin:Josephwijaya34@192.168.1.156:554/stream1", "lapangan": 2}
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

def check_ffmpeg():
    """Check if FFmpeg is available"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False

def convert_to_mp4(input_file, output_file):
    """Convert video file to MP4 using FFmpeg"""
    try:
        if not check_ffmpeg():
            print("[CONVERT ERROR] FFmpeg not found. Installing FFmpeg is recommended for better MP4 support.")
            return False
            
        cmd = [
            'ffmpeg', '-i', input_file,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-y',  # Overwrite output file
            output_file
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[CONVERT SUCCESS] Successfully converted {input_file} to {output_file}")
            # Remove temporary file
            if os.path.exists(input_file) and input_file != output_file:
                os.remove(input_file)
            return True
        else:
            print(f"[CONVERT ERROR] FFmpeg failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"[CONVERT ERROR] Exception during conversion: {e}")
        return False

def get_cameras_by_court(court_id):
    """Get all cameras that belong to a specific court/lapangan"""
    matching_cameras = []
    for i, cam_info in enumerate(cameras):
        if cam_info.get('lapangan') == court_id:
            matching_cameras.append((i, cam_info))
    return matching_cameras

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
        """Improved capture loop with better MP4 handling"""
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

                # 4. LOGIKA REKAMAN CCTV - Use AVI first, then convert to MP4
                if cctv_writer is None or time.time() > cctv_end_time:
                    if cctv_writer is not None:
                        cctv_writer.release()
                        print(f"[CCTV FINISHED] Segmen rekaman sebelumnya untuk kamera ID {self.camera_id} selesai.")
                    
                    height, width, _ = frame.shape
                    fourcc = cv2.VideoWriter_fourcc(*'XVID')  # Use XVID for reliable recording
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    temp_filename = f"temp_clips/{self.cam_info['name'].replace(' ', '_')}_{timestamp}.avi"
                    final_filename = f"recordings/{self.cam_info['name'].replace(' ', '_')}_{timestamp}.mp4"
                    
                    cctv_writer = cv2.VideoWriter(temp_filename, fourcc, self.fps, (width, height))
                    cctv_end_time = time.time() + CCTV_CHUNK_MINUTES * 60
                    print(f"[CCTV STARTED] Mulai merekam segmen untuk kamera ID {self.camera_id} ke file {temp_filename}")
                    
                    # Schedule conversion to MP4 when recording ends
                    self.current_temp_file = temp_filename
                    self.current_final_file = final_filename

                cctv_writer.write(frame)

            except Exception as e:
                print(f"[CAPTURE FATAL ERROR] Exception di loop utama kamera {self.camera_id}: {e}")
                time.sleep(10)

    def create_pre_event_clip(self, requested_duration):
        """Create clip with proper MP4 conversion"""
        if not self.is_online or not self.buffer:
            print(f"[CLIP ERROR] Tidak bisa membuat klip, kamera ID {self.camera_id} sedang offline atau buffer kosong.")
            return

        if requested_duration > MAX_PRE_EVENT_SECONDS:
            print(f"[CLIP WARNING] Durasi {requested_duration}s melebihi buffer. Dibatasi menjadi {MAX_PRE_EVENT_SECONDS}s.")
            requested_duration = MAX_PRE_EVENT_SECONDS

        print(f"[CLIP] Memulai pembuatan klip {requested_duration}s (Pre-Event) untuk kamera ID {self.camera_id}")
        
        all_frames = []
        with self.buffer_lock:
            frames_to_take = self.fps * requested_duration
            all_frames = list(self.buffer)[-frames_to_take:]

        if not all_frames:
            print("[CLIP ERROR] Tidak ada frame yang bisa diproses setelah mengambil dari buffer.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Create temporary AVI file first
        temp_filename = f"temp_clips/{self.cam_info['name'].replace(' ', '_')}_{requested_duration}s_{timestamp}.avi"
        final_filename = f"clips/{self.cam_info['name'].replace(' ', '_')}_{requested_duration}s_{timestamp}.mp4"
        
        height, width, _ = all_frames[0].shape
        fourcc = cv2.VideoWriter_fourcc(*'XVID')  # Use reliable XVID codec
        out = cv2.VideoWriter(temp_filename, fourcc, self.fps, (width, height))
        
        if not out.isOpened():
            print(f"[CLIP ERROR] Gagal membuka video writer untuk {temp_filename}")
            return
        
        for frame in all_frames:
            out.write(frame)
        
        out.release()
        print(f"[CLIP] Temporary AVI file created: {temp_filename}")
        
        # Convert to MP4
        if convert_to_mp4(temp_filename, final_filename):
            filename_to_upload = final_filename
            print(f"[CLIP SUCCESS] MP4 clip berhasil dibuat: {final_filename}")
        else:
            # If conversion fails, use the AVI file but rename it
            fallback_filename = f"clips/{self.cam_info['name'].replace(' ', '_')}_{requested_duration}s_{timestamp}.avi"
            os.rename(temp_filename, fallback_filename)
            filename_to_upload = fallback_filename
            print(f"[CLIP FALLBACK] Using AVI format: {fallback_filename}")
        
        # Upload the clip
        upload_thread = threading.Thread(
            target=upload_clip_task, 
            args=(filename_to_upload, self.cam_info.get('name', 'N/A'), self.cam_info.get('lapangan', 0), datetime.now().isoformat())
        )
        upload_thread.daemon = True
        upload_thread.start()

# Inisialisasi semua kamera
advanced_cameras = {i: AdvancedCamera(i, cam_info) for i, cam_info in enumerate(cameras)}

# --- FUNGSI UNTUK MULTI-CAMERA CLIPPING ---
def create_clips_for_court(court_id, duration=15):
    """Create clips for all cameras in a specific court"""
    matching_cameras = get_cameras_by_court(court_id)
    
    if not matching_cameras:
        print(f"[MULTI-CLIP WARNING] Tidak ada kamera ditemukan untuk lapangan {court_id}")
        return {"success": False, "message": f"No cameras found for court {court_id}"}
    
    print(f"[MULTI-CLIP] Memulai pembuatan klip untuk lapangan {court_id}. Kamera ditemukan: {len(matching_cameras)}")
    
    clip_threads = []
    results = []
    
    for camera_id, cam_info in matching_cameras:
        if camera_id in advanced_cameras:
            print(f"[MULTI-CLIP] Memicu klip untuk kamera ID {camera_id} ({cam_info['name']})")
            
            clip_thread = threading.Thread(
                target=advanced_cameras[camera_id].create_pre_event_clip,
                args=(duration,)
            )
            clip_thread.daemon = True
            clip_thread.start()
            clip_threads.append(clip_thread)
            
            results.append({
                "camera_id": camera_id,
                "camera_name": cam_info['name'],
                "status": "started"
            })
        else:
            print(f"[MULTI-CLIP WARNING] Kamera ID {camera_id} tidak tersedia")
            results.append({
                "camera_id": camera_id,
                "camera_name": cam_info['name'],
                "status": "unavailable"
            })
    
    return {
        "success": True,
        "message": f"Started clip creation for {len(clip_threads)} cameras in court {court_id}",
        "cameras": results
    }

# --- FUNGSI UPLOAD & LISTENER UDP ---
def upload_clip_task(filename, camera_name, lapangan, start_time):
    """Mengirim file video dan metadata ke endpoint API"""
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
            "courtId": int(court_id),
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

        # 3. Upload video clip
        file_size = os.path.getsize(filename)
        print(f"[UPLOAD INFO] File size: {file_size} bytes")
        
        if file_size > 100 * 1024 * 1024:  # 100MB limit
            print(f"[UPLOAD ERROR] File too large: {file_size} bytes")
            return
            
        with open(filename, 'rb') as f:
            clip_name = os.path.basename(filename)
            
            # Determine MIME type based on file extension
            file_ext = os.path.splitext(filename)[1].lower()
            if file_ext == '.mp4':
                mime_type = 'video/mp4'
            elif file_ext == '.avi':
                mime_type = 'video/x-msvideo'
            elif file_ext == '.webm':
                mime_type = 'video/webm'
            else:
                mime_type = 'video/mp4'  # default to mp4
            
            files = {'video': (clip_name, f, mime_type)}
            payload = {'bookingHourId': str(booking_hour_id)}
            
            print(f"[UPLOAD DEBUG] Uploading {clip_name} ({mime_type})")
            
            try:
                response = requests.post(
                    CLIP_UPLOAD_ENDPOINT, 
                    files=files, 
                    data=payload, 
                    timeout=120
                )
                
                if response.status_code in [200, 201]:
                    print(f"[UPLOAD SUCCESS] Successfully uploaded {filename}")
                    # Optionally delete local file after successful upload
                    # os.remove(filename)
                else:
                    print(f"[UPLOAD FAILED] Failed to upload {filename}")
                    print(f"[UPLOAD FAILED] Status: {response.status_code}")
                    print(f"[UPLOAD FAILED] Response: {response.text}")
                        
            except requests.exceptions.RequestException as e:
                print(f"[UPLOAD ERROR] Request exception: {e}")

    except Exception as e:
        print(f"[UPLOAD FATAL ERROR] Exception in upload_clip_task: {e}")
        import traceback
        traceback.print_exc()

def udp_listener():
    UDP_IP = "0.0.0.0"
    UDP_PORT = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    print(f"✅ Server UDP aktif, mendengarkan di port {UDP_PORT}...")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            message = data.decode('utf-8').strip()
            print(f"\n[UDP RECEIVED] Pesan diterima dari {addr}: {message}")

            try:
                court_number = int(message)
                result = create_clips_for_court(court_number, 15)
                
                if result["success"]:
                    print(f"➡️  Aksi: {result['message']}")
                    response = f"ACK: {result['message']}"
                    sock.sendto(response.encode('utf-8'), addr)
                else:
                    print(f"[UDP WARNING] {result['message']}")
                    response = f"ERROR: {result['message']}"
                    sock.sendto(response.encode('utf-8'), addr)

            except ValueError:
                print(f"[UDP INFO] Pesan tidak dikenali atau bukan nomor lapangan: '{message}'")
                response = f"NACK: Unknown command '{message}'. Expected a court number."
                sock.sendto(response.encode('utf-8'), addr)
        except Exception as e:
            print(f"[UDP ERROR] Terjadi error di listener UDP: {e}")

# --- RUTE-RUTE FLASK ---
def generate_frame(camera_id):
    """Mengambil frame terakhir dari buffer"""
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

@app.route('/fullscreen/<int:camera_id>')
@login_required
def fullscreen(camera_id):
    if camera_id >= len(cameras):
        return redirect(url_for('index'))
    return render_template('fullscreen.html', camera=cameras[camera_id], camera_id=camera_id)

@app.route('/')
@login_required
def index():
    cameras_with_index = list(enumerate(cameras))
    current_recording_state = {i: True for i in range(len(cameras))}
    
    # Group cameras by court for display
    courts_data = {}
    for i, cam_info in enumerate(cameras):
        court_id = cam_info.get('lapangan', 0)
        if court_id not in courts_data:
            courts_data[court_id] = []
        courts_data[court_id].append((i, cam_info))
    
    return render_template('index.html', 
                         cameras=cameras, 
                         cameras_with_index=cameras_with_index,
                         recording_state=current_recording_state,
                         courts_data=courts_data)

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

# NEW: Multi-camera clip endpoint
@app.route('/clip_court/<int:court_id>/<int:duration>')
@login_required
def clip_court(court_id, duration=15):
    """Create clips for all cameras in a specific court"""
    result = create_clips_for_court(court_id, duration)
    
    if result["success"]:
        return jsonify(result)
    else:
        return jsonify(result), 404

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
    # Check if FFmpeg is available
    if check_ffmpeg():
        print("✅ FFmpeg detected - MP4 conversion will be used")
    else:
        print("⚠️  FFmpeg not found - clips will be saved as AVI files")
        print("   To install FFmpeg: https://ffmpeg.org/download.html")
    
    listener_thread = threading.Thread(target=udp_listener, daemon=True)
    listener_thread.start()
    
    print("🎥 Advanced RTSP Camera Surveillance System")
    print("=" * 50)
    print("📋 Features:")
    print("   • Pre-event clip recording (single camera & multi-camera)")
    print("   • Proper MP4 conversion with FFmpeg")
    print("   • Continuous CCTV recording")
    print("   • Auto-upload clips to API endpoint")
    print("   • Multi-camera clipping per court")
    print()
    print("🔐 Default Login:")
    print(f"   Username: {default_username}")
    print(f"   Password: {default_password}")
    print()
    print("🌐 Starting server...")
    print("   Access at: http://127.0.0.1:5001")
    print("=" * 50)
    
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True, use_reloader=False)