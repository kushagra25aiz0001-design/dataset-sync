"""
Camera Recorder
===============
Captures video frames from a USB camera with precise timestamps
for synchronized multi-modal recording.

Runs in its own thread, started by sync_manager.py.
"""

import os
import csv
import time
import threading

import cv2


class CameraRecorder:
    """Records camera frames with monotonic timestamps."""

    def __init__(self, session, device_id: int = 0, fps: int = 30,
                 resolution: tuple = (640, 480), save_format: str = "frames"):
        self.session = session
        self.device_id = device_id
        self.fps = fps
        self.resolution = resolution
        self.save_format = save_format  # "frames" or "video"

        self.output_dir = session.camera_dir
        self.frame_count = 0
        self._stop_event = threading.Event()
        self._thread = None

        # Update session metadata
        session.update_device_config("camera", {
            "device_id": device_id,
            "resolution": list(resolution),
            "fps": fps,
            "save_format": save_format
        })

    def start(self):
        """Start recording in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop recording and wait for thread to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _record_loop(self):
        """Main recording loop — runs in thread."""
        cap = cv2.VideoCapture(self.device_id)
        if not cap.isOpened():
            print(f"[CAMERA] ERROR: Cannot open camera device {self.device_id}")
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        cap.set(cv2.CAP_PROP_FPS, self.fps)

        # Actual values (camera may not support requested)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        print(f"[CAMERA] Opened: {actual_w}x{actual_h} @ {actual_fps:.1f} FPS")

        # Video writer (if saving as video)
        video_writer = None
        if self.save_format == "video":
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_path = os.path.join(self.output_dir, "video.mp4")
            video_writer = cv2.VideoWriter(video_path, fourcc, self.fps,
                                           (actual_w, actual_h))

        # Timestamp CSV
        ts_path = os.path.join(self.output_dir, "timestamps.csv")
        ts_file = open(ts_path, "w", newline="")
        ts_writer = csv.writer(ts_file)
        ts_writer.writerow(["frame_idx", "timestamp_s", "filename"])

        frame_interval = 1.0 / self.fps
        next_frame_time = time.monotonic()

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()

                # Rate limiting
                if now < next_frame_time:
                    time.sleep(max(0, next_frame_time - now - 0.001))
                    continue

                ret, frame = cap.read()
                if not ret:
                    continue

                timestamp = time.monotonic() - self.session.t0
                frame_name = f"frame_{self.frame_count:06d}.jpg"

                if self.save_format == "frames":
                    frame_path = os.path.join(self.output_dir, frame_name)
                    cv2.imwrite(frame_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                elif video_writer:
                    video_writer.write(frame)

                ts_writer.writerow([self.frame_count, f"{timestamp:.4f}", frame_name])
                self.frame_count += 1
                next_frame_time += frame_interval

        except Exception as e:
            print(f"[CAMERA] ERROR: {e}")
        finally:
            cap.release()
            if video_writer:
                video_writer.release()
            ts_file.close()
            print(f"[CAMERA] Stopped. {self.frame_count} frames captured.")
