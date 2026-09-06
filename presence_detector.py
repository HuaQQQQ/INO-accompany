import ctypes
import socket
import sys
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError:
        AudioUtilities = None
else:
    AudioUtilities = None

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

if IS_WINDOWS:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
else:
    class LASTINPUTINFO:
        pass

class PresenceDetector:
    def __init__(self, config=None):
        self.config = config or {}
        presence_cfg = self.config.get("presence", {})
        self.idle_threshold = presence_cfg.get("idle_threshold", 300)
        self.enable_audio = presence_cfg.get("enable_audio_detection", True)
        self.enable_fullscreen = presence_cfg.get("enable_fullscreen_suppress", True)
        self.enable_camera = presence_cfg.get("enable_camera_detection", True)
        self.primary_pc_ip = presence_cfg.get("primary_pc_ip", "127.0.0.1")
        self.last_state = "active"

        # Camera detection cache
        self.last_camera_check = 0.0
        self.camera_check_interval = presence_cfg.get("camera_check_interval", 15.0) # Check every 15s
        self.last_human_detected = True

        self.face_cascade = None
        if OPENCV_AVAILABLE:
            try:
                cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
                self.face_cascade = cv2.CascadeClassifier(cascade_path)
            except Exception as e:
                print(f"Haar cascade load warning: {e}")

    def is_human_present_by_camera(self) -> bool:
        if not OPENCV_AVAILABLE or not self.enable_camera or self.face_cascade is None:
            return True # Fall back to input/socket if camera disabled
        
        now = time.time()
        # Return cached result if within interval
        if now - self.last_camera_check < self.camera_check_interval:
            return self.last_human_detected
        
        self.last_camera_check = now
        try:
            # Briefly open webcam with DirectShow backend for fast single-frame sampling
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if cap.isOpened():
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    faces = self.face_cascade.detectMultiScale(
                        gray,
                        scaleFactor=1.1,
                        minNeighbors=4,
                        minSize=(40, 40)
                    )
                    self.last_human_detected = (len(faces) > 0)
                    return self.last_human_detected
            cap.release()
        except Exception as e:
            print(f"Camera detection warning: {e}")

        return self.last_human_detected

    def get_input_idle_seconds(self) -> float:
        if not IS_WINDOWS:
            return 0.0
        try:
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                return millis / 1000.0
        except Exception:
            pass
        return 0.0

    def is_session_locked(self) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            user32 = ctypes.windll.user32
            hDesktop = user32.OpenInputDesktop(0, False, 0x0100) # DESKTOP_SWITCHDESKTOP
            if not hDesktop:
                return True
            user32.CloseDesktop(hDesktop)
        except Exception:
            pass
        return False

    def is_fullscreen(self) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            rect = (ctypes.c_long * 4)()
            user32.GetWindowRect(hwnd, rect)
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            return w >= sw and h >= sh
        except Exception:
            pass
        return False

    def is_audio_playing(self) -> bool:
        if not self.enable_audio or not AudioUtilities:
            return False
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.State == 1: # AudioSessionStateActive
                    return True
        except Exception:
            pass
        return False

    def is_primary_pc_online(self) -> bool:
        if not self.primary_pc_ip:
            return False
        for port in [1234, 445]:
            try:
                s = socket.create_connection((self.primary_pc_ip, port), timeout=0.3)
                s.close()
                return True
            except Exception:
                pass
        return False

    def evaluate_state(self) -> str:
        # 1. Lock screen or sleep -> Away
        if self.is_session_locked():
            current = "away"
        else:
            idle_sec = self.get_input_idle_seconds()
            audio_active = self.is_audio_playing()
            primary_online = self.is_primary_pc_online()
            human_detected = self.is_human_present_by_camera()

            if self.enable_fullscreen and self.is_fullscreen():
                current = "immersive"
            elif idle_sec <= 10:
                current = "active"
            elif human_detected and (audio_active or primary_online or idle_sec < self.idle_threshold):
                current = "desk_companion"
            else:
                # No human face detected in front of camera -> Away (sleeping/left desk)
                current = "away"

        state_to_return = current
        if self.last_state == "away" and current in ["active", "desk_companion"]:
            state_to_return = "returned"

        self.last_state = current
        return state_to_return
