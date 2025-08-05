# 🎥 RTSP Camera Surveillance System

A modern, feature-rich web-based RTSP camera surveillance system built with Flask, OpenCV, and Tailwind CSS. Monitor multiple IP cameras with real-time streaming, recording, snapshots, and video clips.

![Dashboard Preview](https://via.placeholder.com/800x400/667eea/FFFFFF?text=Modern+Camera+Dashboard)

## ✨ Features

### 🎯 Core Functionality
- **Multi-Camera Support** - Monitor up to 4+ RTSP cameras simultaneously
- **Real-time Streaming** - Live video feeds with minimal latency
- **Recording Control** - Start/stop recording per camera or all cameras
- **Instant Snapshots** - Capture and save images from any camera
- **Video Clips** - Generate 15s, 30s, or 60s video clips on demand
- **Fullscreen View** - Dedicated fullscreen mode for individual cameras

### 🎨 Modern UI/UX
- **Responsive Design** - Works perfectly on desktop, tablet, and mobile
- **Glass Morphism** - Modern glass effect styling
- **Dark Theme** - Eye-friendly dark interface
- **Real-time Status** - Live camera status indicators
- **Smooth Animations** - Polished hover effects and transitions
- **Interactive Controls** - Intuitive button controls with feedback

### 🔒 Security & Management
- **Secure Authentication** - Login system with password hashing
- **Session Management** - Secure session handling
- **Password Change** - Built-in password update functionality
- **Access Control** - Protected routes and API endpoints

### 📊 Monitoring & Status
- **Camera Status** - Real-time online/offline detection
- **Recording Indicators** - Visual recording status per camera
- **System Stats** - Dashboard overview with counts
- **Timestamp Overlay** - Live timestamp on video streams
- **Error Handling** - Graceful handling of camera disconnections

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- OpenCV compatible system
- RTSP-enabled IP cameras

### Installation

1. **Clone or Download** the application files
2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure Cameras** - Edit the camera URLs in `app.py`:
   ```python
   cameras = [
       {"name": "Front Door", "url": "rtsp://admin:password@192.168.1.127:554/stream1"},
       {"name": "Back Yard", "url": "rtsp://admin:password@192.168.1.128:554/stream1"},
       # Add more cameras as needed
   ]
   ```
4. **Run the Application**:
   ```bash
   python app.py
   ```
5. **Access Dashboard** - Open http://localhost:5000 in your browser

### Default Login
- **Username**: `admin`
- **Password**: `admin123`

## 📁 Project Structure

```
rtsp-camera-app/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── templates/
│   ├── base.html         # Base template with navigation
│   ├── index.html        # Main dashboard
│   ├── fullscreen.html   # Fullscreen camera view
│   ├── login.html        # Login page
│   └── change_password.html # Password change form
├── snapshots/            # Saved snapshots (auto-created)
├── recordings/           # Video recordings (auto-created)
└── clips/               # Video clips (auto-created)
```

## 🎮 Usage Guide

### Dashboard Operations
- **View Streams** - All cameras display live feeds in grid layout
- **Take Snapshots** - Click camera snapshot button or use "All Snapshots"
- **Start Recording** - Click record button (turns red when recording)
- **Create Clips** - Choose 15s, 30s, or 60s clip buttons
- **Fullscreen Mode** - Click expand icon on any camera

### Fullscreen View
- **Keyboard Shortcuts**:
  - `R` - Toggle recording
  - `S` - Take snapshot
  - `F` - Toggle fullscreen
  - `ESC` - Exit fullscreen/close window
- **Controls** - Full control panel at bottom
- **Quality Settings** - Adjust stream quality
- **Stream Info** - Real-time stream statistics

### Global Controls
- **Record All** - Start recording on all cameras simultaneously
- **Stop All** - Stop all active recordings
- **All Snapshots** - Capture images from all cameras

## ⚙️ Configuration

### Camera Settings
Edit the `cameras` list in `app.py`:
```python
cameras = [
    {
        "name": "Camera Name",
        "url": "rtsp://username:password@ip:port/stream"
    }
]
```

### RTSP URL Formats
Common RTSP URL patterns:
- **Generic**: `rtsp://username:password@ip:port/stream1`
- **Hikvision**: `rtsp://admin:password@ip:554/Streaming/Channels/101`
- **Dahua**: `rtsp://admin:password@ip:554/cam/realmonitor?channel=1&subtype=0`
- **Axis**: `rtsp://root:password@ip/axis-media/media.amp`

### File Storage
All media files are automatically organized:
- **Snapshots**: `snapshots/CameraName_YYYYMMDD_HHMMSS.jpg`
- **Recordings**: `recordings/camera_X_YYYYMMDD_HHMMSS.avi`
- **Clips**: `clips/CameraName_XSs_YYYYMMDD_HHMMSS.avi`

## 🔧 Advanced Configuration

### Custom Camera Count
Add more cameras by extending the `cameras` list. The UI automatically adapts to any number of cameras.

### Recording Settings
Modify recording parameters in the `CameraRecorder` class:
```python
fps = int(cap.get(cv2.CAP_PROP_FPS)) or 20  # Frame rate
fourcc = cv2.VideoWriter_fourcc(*'XVID')    # Codec
```

### Stream Quality
Adjust stream quality in `generate_frame()`:
```python
ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
```

### Authentication
Change default credentials:
```python
user_data = {
    "username": "your_username",
    "password": generate_password_hash("your_password")
}
```

## 🐛 Troubleshooting

### Common Issues

**Camera Not Connecting**
- Verify RTSP URL format
- Check camera credentials
- Ensure camera is accessible on network
- Test with VLC media player first

**Stream Loading Slowly**
- Reduce video quality in camera settings
- Check network bandwidth
- Adjust buffer size in code

**Recording Not Working**
- Verify write permissions for directories
- Check available disk space
- Ensure codec compatibility

### Debug Mode
Enable detailed logging by setting:
```python
app.run(debug=True)
```

## 🚀 Deployment

### Production Deployment
1. **Use Gunicorn**:
   ```bash
   gunicorn -w 4 -b 0.0.0.0:5000 app:app
   ```
2. **Set Environment**:
   ```bash
   export FLASK_ENV=production
   ```
3. **Configure Reverse Proxy** (Nginx recommended)
4. **Set Strong Passwords** and secure session keys

### Docker Deployment
```dockerfile
FROM python:3.9-slim
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
EXPOSE 5000
CMD ["python", "app.py"]
```

## 📋 API Endpoints

### Authentication
- `GET/POST /login` - User login
- `GET /logout` - User logout
- `GET/POST /change_password` - Password management

### Camera Control
- `GET /` - Main dashboard
- `GET /fullscreen/<camera_id>` - Fullscreen view
- `GET /video_feed/<camera_id>` - Video stream

### Actions
- `GET /snapshot/<camera_id>` - Take single snapshot
- `GET /snapshot_all` - Take all snapshots
- `GET /start_recording/<camera_id>` - Start recording
- `GET /stop_recording/<camera_id>` - Stop recording
- `GET /clip/<camera_id>/<duration>` - Create video clip

### Status API
- `GET /api/camera_status` - Camera status JSON
- `GET /api/recording_status` - Recording status JSON

## 🤝 Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
