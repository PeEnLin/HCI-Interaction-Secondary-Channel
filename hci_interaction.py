"""HCI Interaction: Secondary-Channel Touchless Interface.

Implements a 4-stage intentionality pipeline using head pose tracking and
cross-modal spatial audio feedback for dual-task secondary-channel control.
"""

from collections import deque
import time
import cv2
import mediapipe as mp
import numpy as np
import pygame
from pynput.keyboard import Controller, Key


class SpatialAudio:
    """Synthesizes low-latency binaural stereo sine wave pulses."""

    def __init__(self, sample_rate: int = 44100):
        pygame.mixer.init(frequency=sample_rate, size=-16, channels=2, buffer=512)
        self.sample_rate = sample_rate
        self.snd_left = self._gen_tone(freq=440.0, channel="left")
        self.snd_right = self._gen_tone(freq=880.0, channel="right")

    def _gen_tone(self, freq: float, channel: str, duration: float = 0.12) -> pygame.mixer.Sound:
        t = np.linspace(0, duration, int(self.sample_rate * duration), endpoint=False)
        sine = np.sin(2.0 * np.pi * freq * t)
        fade = int(self.sample_rate * 0.012)
        env = np.ones_like(sine)
        env[:fade], env[-fade:] = np.linspace(0, 1, fade), np.linspace(1, 0, fade)
        pcm = (sine * env * 32767 * 0.55).astype(np.int16)
        stereo = np.zeros((len(pcm), 2), dtype=np.int16)
        stereo[:, 0 if channel == "left" else 1] = pcm
        return pygame.sndarray.make_sound(stereo)

    def play_left(self):
        self.snd_left.play()

    def play_right(self):
        self.snd_right.play()


class HeadPoseTracker:
    """Uses MediaPipe FaceMesh to track facial landmarks and compute normalized horizontal Yaw."""

    def __init__(self):
        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.5, min_tracking_confidence=0.5
        )

    def process_frame(self, frame: np.ndarray):
        """Processes BGR frame and returns (yaw_deg, key_points)."""
        h, w = frame.shape[:2]
        res = self.face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            return None, None

        lm = res.multi_face_landmarks[0].landmark
        nose = np.array([lm[1].x * w, lm[1].y * h])
        l_cheek = np.array([lm[234].x * w, lm[234].y * h])
        r_cheek = np.array([lm[454].x * w, lm[454].y * h])

        # Yaw from horizontal landmark ratio
        x_min, x_max = min(l_cheek[0], r_cheek[0]), max(l_cheek[0], r_cheek[0])
        d_left, d_right = nose[0] - x_min, x_max - nose[0]
        denom = d_left + d_right
        if denom < 1e-4:
            return 0.0, None

        yaw_deg = ((d_left - d_right) / denom) * 55.0
        key_pts = {
            "nose": tuple(nose.astype(int)),
            "l_cheek": tuple(l_cheek.astype(int)),
            "r_cheek": tuple(r_cheek.astype(int)),
            "l_eye": (int(lm[33].x * w), int(lm[33].y * h)),
            "r_eye": (int(lm[263].x * w), int(lm[263].y * h)),
            "chin": (int(lm[152].x * w), int(lm[152].y * h)),
        }
        return yaw_deg, key_pts


class SignalFilter:
    """Strictly implements the 4-stage intentionality pipeline."""

    def __init__(self, calib_frames: int = 60, deadband: float = 6.0,
                 dwell_thresh: float = 14.0, dwell_time: float = 0.35, cooldown: float = 0.60):
        self.calib_frames, self.deadband = calib_frames, deadband
        self.dwell_thresh, self.dwell_time, self.cooldown = dwell_thresh, dwell_time, cooldown
        self.calib_buf = deque(maxlen=calib_frames)
        self.baseline_yaw, self.is_calibrated = 0.0, False
        self.dwell_start, self.dwell_dir = None, None
        self.cooldown_until, self.lock_neutral = 0.0, False

    def reset_calibration(self):
        """Reset baseline calibration."""
        self.calib_buf.clear()
        self.is_calibrated, self.dwell_start, self.dwell_dir = False, None, None
        self.lock_neutral = False

    def update(self, raw_yaw: float, now: float):
        """Evaluates raw yaw through the 4-stage intentionality pipeline."""
        # Stage 1: Neutral Baseline Calibration (first 60 frames rolling mean)
        if not self.is_calibrated:
            self.calib_buf.append(raw_yaw)
            if len(self.calib_buf) >= self.calib_frames:
                self.baseline_yaw = float(np.mean(self.calib_buf))
                self.is_calibrated = True
            return None, f"CALIBRATING ({len(self.calib_buf)}/{self.calib_frames})", 0.0, len(self.calib_buf) / self.calib_frames

        delta_theta = raw_yaw - self.baseline_yaw

        # Stage 2: Dynamic Deadband Filter (|Δθ| < 6.0° clamped to 0.0°)
        eff_delta = 0.0 if abs(delta_theta) < self.deadband else delta_theta

        # Stage 4: Hysteresis Cooldown (600ms refractory lock)
        if now < self.cooldown_until:
            return None, f"COOLDOWN ({self.cooldown_until - now:.1f}s)", delta_theta, 0.0

        if self.lock_neutral:
            if eff_delta == 0.0:
                self.lock_neutral = False
            else:
                return None, "RETURN TO CENTER", delta_theta, 0.0

        # Stage 3: Temporal Dwell-Time Integration (>= 14.0° held for >= 350ms)
        candidate = "RIGHT" if eff_delta >= self.dwell_thresh else ("LEFT" if eff_delta <= -self.dwell_thresh else None)

        # Instant abort on premature return
        if candidate is None:
            self.dwell_start, self.dwell_dir = None, None
            return None, "READY", delta_theta, 0.0

        if self.dwell_start is None or self.dwell_dir != candidate:
            self.dwell_start, self.dwell_dir = now, candidate

        elapsed = now - self.dwell_start
        progress = min(1.0, elapsed / self.dwell_time)

        if elapsed >= self.dwell_time:
            action = self.dwell_dir
            self.cooldown_until = now + self.cooldown
            self.lock_neutral, self.dwell_start, self.dwell_dir = True, None, None
            return action, f"TRIGGERED {action}", delta_theta, 1.0

        return None, f"DWELLING {candidate}", delta_theta, progress


class HUDVisualizer:
    """Renders mirrored feed with glassmorphism HUD telemetry and document card."""

    DOC_PAGES = [
        ("PRE-OP CHECKLIST", ["Patient ID: Verified #8491", "Pre-Op Fasting: Compliant", "Antibiotic Prophylaxis: OK"]),
        ("ANESTHESIA INDUCTION", ["Airway: Intubated Grade 1", "EtCO2: 36 mmHg | SpO2: 99%", "Hemodynamics: Stable"]),
        ("LAPAROSCOPIC ACCESS", ["Pneumoperitoneum: 12 mmHg", "Trocar Insertion: 4 Ports", "Laparoscope View: Pristine"]),
        ("DISSECTION & HEMOSTASIS", ["Target Vessel: Clipped x2", "Electrocautery: Monopolar 30W", "Hemostatic Suture: OK"]),
        ("POST-OP & WOUND CLOSURE", ["Sponge/Needle Count: 100%", "Desufflation: Executed", "Subcuticular Suture: Intact"]),
    ]

    def __init__(self):
        self.doc_page, self.flash_alpha, self.flash_color = 0, 0.0, (0, 255, 0)

    def trigger_feedback(self, action: str):
        if action == "RIGHT":
            self.doc_page = min(len(self.DOC_PAGES) - 1, self.doc_page + 1)
            self.flash_color = (50, 255, 120)
        elif action == "LEFT":
            self.doc_page = max(0, self.doc_page - 1)
            self.flash_color = (255, 180, 50)
        self.flash_alpha = 1.0

    def render(self, frame: np.ndarray, key_pts: dict, status: str, delta_theta: float, progress: float):
        h, w = frame.shape[:2]
        canvas = frame.copy()

        if key_pts:
            cv2.line(canvas, key_pts["l_cheek"], key_pts["r_cheek"], (80, 80, 80), 1)
            cv2.circle(canvas, key_pts["nose"], 4, (0, 255, 255), -1)
            cv2.circle(canvas, key_pts["l_eye"], 3, (255, 200, 0), -1)
            cv2.circle(canvas, key_pts["r_eye"], 3, (255, 200, 0), -1)
            cv2.circle(canvas, key_pts["chin"], 3, (120, 120, 120), -1)

        # Top-Right Document Simulator Card
        cw, ch = 300, 125
        cx, cy = w - cw - 20, 20
        overlay = canvas.copy()
        cv2.rectangle(overlay, (cx, cy), (cx + cw, cy + ch), (20, 24, 30), -1)
        cv2.addWeighted(overlay, 0.82, canvas, 0.18, 0, canvas)
        border_col = (0, 255, 255) if "TRIGGER" in status else (70, 75, 90)
        cv2.rectangle(canvas, (cx, cy), (cx + cw, cy + ch), border_col, 1)

        title, bullets = self.DOC_PAGES[self.doc_page]
        cv2.putText(canvas, f"PAGE {self.doc_page + 1} / {len(self.DOC_PAGES)}", (cx + 12, cy + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.95, (0, 255, 200), 2, cv2.LINE_AA)
        cv2.putText(canvas, "DUAL-TASK", (cx + cw - 95, cy + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (160, 170, 190), 1, cv2.LINE_AA)
        cv2.line(canvas, (cx + 10, cy + 36), (cx + cw - 10, cy + 36), (50, 55, 70), 1)
        cv2.putText(canvas, title, (cx + 12, cy + 54), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 245), 1, cv2.LINE_AA)
        for i, b in enumerate(bullets):
            cv2.putText(canvas, f"- {b}", (cx + 12, cy + 74 + i * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 190, 205), 1, cv2.LINE_AA)

        if self.flash_alpha > 0.05:
            fl = canvas.copy()
            cv2.rectangle(fl, (cx, cy), (cx + cw, cy + ch), self.flash_color, -1)
            cv2.addWeighted(fl, self.flash_alpha * 0.35, canvas, 1 - self.flash_alpha * 0.35, 0, canvas)
            self.flash_alpha *= 0.82

        # Bottom Telemetry HUD
        hud_x, hud_y, hud_w, hud_h = 20, h - 130, w - 40, 110
        overlay = canvas.copy()
        cv2.rectangle(overlay, (20, h - 130), (w - 20, h - 20), (15, 18, 24), -1)
        cv2.addWeighted(overlay, 0.85, canvas, 0.15, 0, canvas)
        cv2.rectangle(canvas, (20, h - 130), (w - 20, h - 20), (50, 60, 80), 1)

        st_color = (0, 255, 120) if "READY" in status else ((0, 220, 255) if "DWELL" in status else ((50, 255, 50) if "TRIGGER" in status else (0, 165, 255)))
        cv2.putText(canvas, f"STATUS: {status}", (hud_x + 20, hud_y + 44), cv2.FONT_HERSHEY_SIMPLEX, 0.85, st_color, 2, cv2.LINE_AA)
        cv2.putText(canvas, f"Yaw: {delta_theta:+.1f} deg  (Target: +/-14.0 deg | Deadband: 6.0 deg)", (hud_x + 20, hud_y + 88), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (180, 190, 205), 2, cv2.LINE_AA)

        # Dynamic Dwell Progress Bar
        bar_w, bar_h = 240, 20
        bar_x = hud_x + hud_w - bar_w - 20
        bar_y = hud_y + (hud_h - bar_h) // 2
        cv2.rectangle(canvas, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (40, 48, 60), -1)
        fill_w = int(bar_w * max(0.0, min(1.0, progress)))
        if fill_w > 0:
            b_col = (0, 220, 255) if "DWELL" in status else ((50, 255, 100) if "TRIGGER" in status else (0, 160, 255))
            cv2.rectangle(canvas, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), b_col, -1)
        cv2.rectangle(canvas, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (90, 105, 130), 2)
        pct = f"DWELL: {int(progress * 100)}%" if "DWELL" in status else f"{int(progress * 100)}%"
        tw = cv2.getTextSize(pct, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0][0]
        cv2.putText(canvas, pct, (bar_x + (bar_w - tw) // 2, bar_y + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

        return canvas


def main():
    """Main capture loop coordinating tracking, filtering, audio, and keyboard dispatch."""
    audio = SpatialAudio()
    tracker = HeadPoseTracker()
    sig_filter = SignalFilter()
    hud = HUDVisualizer()
    keyboard = Controller()

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[HCI ERROR] Unable to access camera.")
        return

    print("\n" + "=" * 60)
    print(" HCI INTERACTION: Secondary-Channel Touchless Interface")
    print(" Press 'q' or 'ESC' to exit | 'c' to recalibrate baseline")
    print("=" * 60 + "\n")

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)  # Mirrored feed
            raw_yaw, key_pts = tracker.process_frame(frame)
            now = time.time()

            if raw_yaw is not None:
                action, status, delta_theta, progress = sig_filter.update(raw_yaw, now)
                if action == "RIGHT":
                    keyboard.press(Key.right)
                    keyboard.release(Key.right)
                    audio.play_right()
                    hud.trigger_feedback("RIGHT")
                elif action == "LEFT":
                    keyboard.press(Key.left)
                    keyboard.release(Key.left)
                    audio.play_left()
                    hud.trigger_feedback("LEFT")
            else:
                status, delta_theta, progress = "FACE NOT DETECTED", 0.0, 0.0

            rendered = hud.render(frame, key_pts, status, delta_theta, progress)
            cv2.imshow("HCI Secondary-Channel Touchless Interface", rendered)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break
            elif key in (ord('c'), ord('r')):
                sig_filter.reset_calibration()

    finally:
        cap.release()
        cv2.destroyAllWindows()
        pygame.mixer.quit()


if __name__ == "__main__":
    main()
