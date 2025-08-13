import socket
import threading


class UDPListener:
    def __init__(self, ip="0.0.0.0", port=12345, court_id=1):
        self.ip = ip
        self.port = port
        self.court_id = court_id
        self.running = False
        self.sock = None
        self.listener_thread = None

    def start(self):
        """Start the UDP listener."""
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.ip, self.port))
        print(f"[UDP] Listening on {self.ip}:{self.port}")

        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()

    def stop(self):
        """Stop the UDP listener."""
        self.running = False
        if self.sock:
            self.sock.close()
        print(f"[UDP] Listener on {self.ip}:{self.port} stopped.")

    def _listen(self):
        """Internal method to handle incoming UDP packets."""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                message = data.decode('utf-8')
                print(f"\n[UDP RECEIVED] From {addr}: {message}")
                self._handle_message(message, addr)
            except OSError:
                break  # Socket closed
            except Exception as e:
                print(f"[UDP ERROR] {e}")

    def _handle_message(self, message, addr):
        """Custom logic to process messages."""
        if message == "CREATE_CLIP":
            camera_to_record = 0
            clip_duration = 15
            print(f"➡️  Action: Triggering pre-event clip ({clip_duration}s) from camera ID {camera_to_record}")
            # Threaded action example:
            # clip_thread = threading.Thread(
            #     target=advanced_cameras[camera_to_record].create_pre_event_clip,
            #     args=(clip_duration,)
            # )
            # clip_thread.start()

            response = f"ACK: Pre-event clip started for camera {camera_to_record}"
            self.sock.sendto(response.encode('utf-8'), addr)