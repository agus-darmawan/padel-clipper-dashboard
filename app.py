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

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Add custom Jinja2 filters
@app.template_filter('enumerate')
def enumerate_filter(iterable):
    return enumerate(iterable)

# Create directories for media files
os.makedirs('snapshots', exist_ok=True)
os.makedirs('recordings', exist_ok=True)
os.makedirs('clips', exist_ok=True)

# Load configuration
try:
    from config import CAMERAS, DEFAULT_USERNAME, DEFAULT_PASSWORD
    cameras = CAMERAS
    default_username = DEFAULT_USERNAME
    default_password = DEFAULT_PASSWORD
except ImportError:
    # Fallback to default configuration
    cameras = [
        {"name": "Front Door", "url": "rtsp://admin:ZESFRO@192.168.1.127:554/stream1"},
        {"name": "Back Yard", "url": "rtsp://admin:ZESFRO@192.168.1.128:554/stream1"},
        {"name": "Garage", "url": "rtsp://admin:ZESFRO@192.168.1.129:110/stream1"},
        {"name": "Living Room", "url": "rtsp://admin:ZESFRO@192.168.1.130:554/stream1"}
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

class CameraRecorder:
    def __init__(self, camera_id, rtsp_url):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.recording = False
        self.writer = None
        
    def start_recording(self):
        if self.recording:
            return False
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recordings/camera_{self.camera_id}_{timestamp}.avi"
        
        cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            return False
            
        # Get frame properties
        fps = int(cap.get(cv2.CAP_PROP_FPS)) or 20
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        self.writer = cv2.VideoWriter(filename, fourcc, fps, (width, height))
        
        self.recording = True
        recording_state[self.camera_id] = True
        
        def record():
            while self.recording:
                ret, frame = cap.read()
                if ret:
                    self.writer.write(frame)
                else:
                    break
                    
            cap.release()
            if self.writer:
                self.writer.release()
                
        thread = threading.Thread(target=record)
        thread.daemon = True
        thread.start()
        return True
        
    def stop_recording(self):
        self.recording = False
        recording_state[self.camera_id] = False
        if self.writer:
            self.writer.release()
            self.writer = None

# Camera recorder instances
recorders = {i: CameraRecorder(i, cameras[i]["url"]) for i in range(len(cameras))}

def generate_frame(rtsp_url, camera_id):
    cap = cv2.VideoCapture(rtsp_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Reduce buffer size for real-time streaming
    
    while True:
        ret, frame = cap.read()
        if not ret:
            # Create error frame
            frame = create_error_frame()
        
        # Add timestamp overlay
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, timestamp, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        
        # Add recording indicator
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

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

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
        
        # Validate current password
        if not check_password_hash(user_data["password"], current_password):
            return render_template('change_password.html', error="Current password is incorrect")
        
        # Validate new password
        if new_password != confirm_password:
            return render_template('change_password.html', error="New passwords do not match")
        
        if len(new_password) < 6:
            return render_template('change_password.html', error="Password must be at least 6 characters long")
        
        # Update password
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
    # Pass enumerated cameras to template
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
    if camera_id >= len(cameras):
        return jsonify({"error": "Camera not found"}), 404
    
    def create_clip():
        cap = cv2.VideoCapture(cameras[camera_id]["url"])
        if not cap.isOpened():
            return False
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"clips/{cameras[camera_id]['name'].replace(' ', '_')}_{duration}s_{timestamp}.avi"
        
        fps = int(cap.get(cv2.CAP_PROP_FPS)) or 20
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out = cv2.VideoWriter(filename, fourcc, fps, (width, height))
        
        frame_count = fps * duration
        for _ in range(frame_count):
            ret, frame = cap.read()
            if ret:
                out.write(frame)
            else:
                break
        
        cap.release()
        out.release()
        return filename
    
    filename = create_clip()
    if filename:
        return jsonify({"message": f"{duration}s clip created successfully", "filename": filename})
    else:
        return jsonify({"error": "Failed to create clip"}), 500

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
    print("🎥 RTSP Camera Surveillance System")
    print("=" * 50)
    print("📋 Features:")
    print("   • Multi-camera RTSP streaming")
    print("   • Real-time recording & snapshots")
    print("   • Fullscreen camera view")
    print("   • Video clip generation")
    print("   • Modern responsive UI")
    print("   • Secure authentication")
    print()
    print("🔐 Default Login:")
    print("   Username: admin")
    print("   Password: admin123")
    print()
    print("🌐 Starting server...")
    print("   Access at: http://localhost:5001")
    print("=" * 50)
    
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True)