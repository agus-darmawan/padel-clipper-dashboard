import json
import os


class ConfigManager:
    def __init__(self, filename='config.json'):
        self.filename = filename
        self.config = self._load_config()

    # ---------------------------
    # Core load/save
    # ---------------------------
    def _load_config(self):
        if not os.path.exists(self.filename):
            print(f"[ERROR] Config file '{self.filename}' not found.")
            return {}
        try:
            with open(self.filename, 'r', encoding='utf-8') as file:
                return json.load(file)
        except json.JSONDecodeError:
            print(f"[ERROR] '{self.filename}' is not valid JSON.")
            return {}
        except Exception as e:
            print(f"[ERROR] Unexpected error loading config: {e}")
            return {}

    def save(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as file:
                json.dump(self.config, file, indent=4)
            print(f"[CONFIG] Saved to '{self.filename}'.")
        except Exception as e:
            print(f"[ERROR] Unexpected error saving config: {e}")

    # ---------------------------
    # Generic key-value
    # ---------------------------
    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save()

    # ---------------------------
    # Camera CRUD
    # ---------------------------
    def list_cameras(self):
        return self.config.get("cameras", [])

    def get_camera(self, court_id):
        for cam in self.list_cameras():
            if cam.get("court_id") == court_id:
                return cam
        return None

    def add_camera(self, url, court_id):
        if self.get_camera(court_id):
            print(f"[ERROR] Camera with court_id {court_id} already exists.")
            return False
        self.config.setdefault("cameras", []).append({
            "url": url,
            "court_id": court_id
        })
        self.save()
        return True

    def update_camera(self, court_id, **kwargs):
        for cam in self.list_cameras():
            if cam.get("court_id") == court_id:
                cam.update(kwargs)
                self.save()
                return True
        print(f"[ERROR] Camera with court_id {court_id} not found.")
        return False

    def delete_camera(self, court_id):
        cameras = self.list_cameras()
        new_cameras = [c for c in cameras if c.get("court_id") != court_id]
        if len(new_cameras) == len(cameras):
            print(f"[ERROR] Camera with court_id {court_id} not found.")
            return False
        self.config["cameras"] = new_cameras
        self.save()
        return True

    # ---------------------------
    # Button CRUD
    # ---------------------------
    def list_buttons(self):
        return self.config.get("buttons", [])

    def get_button(self, court_id):
        for btn in self.list_buttons():
            if btn.get("court_id") == court_id:
                return btn
        return None

    def add_button(self, court_id, ip, port):
        if self.get_button(court_id):
            print(f"[ERROR] Button with court_id {court_id} already exists.")
            return False
        self.config.setdefault("buttons", []).append({
            "court_id": court_id,
            "ip": ip,
            "port": port
        })
        self.save()
        return True

    def update_button(self, court_id, **kwargs):
        for btn in self.list_buttons():
            if btn.get("court_id") == court_id:
                btn.update(kwargs)  # Can update ip, port, or other keys
                self.save()
                return True
        print(f"[ERROR] Button with court_id {court_id} not found.")
        return False


    def delete_button(self, court_id):
        buttons = self.list_buttons()
        new_buttons = [b for b in buttons if b.get("court_id") != court_id]
        if len(new_buttons) == len(buttons):
            print(f"[ERROR] Button with court_id {court_id} not found.")
            return False
        self.config["buttons"] = new_buttons
        self.save()
        return True
