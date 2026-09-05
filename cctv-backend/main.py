import os
import json
import base64
import re
import time
import collections
import threading
from fastapi import FastAPI, File, UploadFile, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import cv2
import numpy as np
import easyocr
import insightface
from insightface.app import FaceAnalysis

app = FastAPI(title="CCTV AI Detection & ANPR Backend", version="2.0.0")

# Enable CORS for React dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================================
# CONFIGURABLE DETECTION & TRACKING THRESHOLDS
# =========================================================================
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"
SAVE_DEBUG_FRAMES = os.getenv("SAVE_DEBUG_FRAMES", "true").lower() == "true"

# Person / Face Detection & Temporal Confirmation Thresholds
FACE_DET_SCORE_THRESHOLD = float(os.getenv("FACE_DET_SCORE_THRESHOLD", "0.25"))
FACE_MATCH_SIM_THRESHOLD = float(os.getenv("FACE_MATCH_SIM_THRESHOLD", "0.25"))
PERSON_CONFIRM_N = int(os.getenv("PERSON_CONFIRM_N", "1"))      # Require N out of M frames to confirm presence (1 = instant)
PERSON_WINDOW_M = int(os.getenv("PERSON_WINDOW_M", "5"))       # M sliding window frame count
PERSON_ABSENT_K = int(os.getenv("PERSON_ABSENT_K", "3"))       # K consecutive absent frames to mark absent


# License Plate ANPR Thresholds
PLATE_MIN_CONF = float(os.getenv("PLATE_MIN_CONF", "0.35"))
PLATE_MIN_CONSENSUS = int(os.getenv("PLATE_MIN_CONSENSUS", "2")) # Min matching reads across window
PLATE_WINDOW_M = int(os.getenv("PLATE_WINDOW_M", "6"))         # Sliding window size for OCR consensus

# 1. Initialize EasyOCR Reader
print("[ANPR Engine] Loading EasyOCR Engine...")
reader = easyocr.Reader(['en'], gpu=False)
print("[ANPR Engine] EasyOCR Engine Ready for Digits & Plates!")

# 2. Smart Face Analyzer with InsightFace & Haar Fallback
class SmartFaceAnalyzer:
    def __init__(self):
        self.real_analyzer = None
        self.fallback_analyzer = None
        self.try_init_real()
        if self.real_analyzer is None:
            self.init_fallback()

    def try_init_real(self):
        try:
            print("[SmartFaceAnalyzer] Initializing InsightFace Buffalo_L model...")
            real = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
            real.prepare(ctx_id=0, det_size=(640, 640))
            self.real_analyzer = real
            print("[SmartFaceAnalyzer] Loaded InsightFace model successfully.")
        except Exception as e:
            print(f"[SmartFaceAnalyzer] Could not load InsightFace model directly: {e}. Trying buffalo_sc...")
            try:
                real = FaceAnalysis(name='buffalo_sc', providers=['CPUExecutionProvider'])
                real.prepare(ctx_id=0, det_size=(640, 640))
                self.real_analyzer = real
                print("[SmartFaceAnalyzer] Loaded InsightFace buffalo_sc successfully.")
            except Exception as ex:
                print(f"[SmartFaceAnalyzer] InsightFace initialization failed: {ex}")
                self.real_analyzer = None

    def init_fallback(self):
        print("[SmartFaceAnalyzer] Initializing OpenCV Haar Cascade face detector fallback...")
        try:
            if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data'):
                self.fallback_analyzer = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            else:
                self.fallback_analyzer = None
        except Exception:
            self.fallback_analyzer = None
        if self.fallback_analyzer is None or getattr(self.fallback_analyzer, 'empty', lambda: True)():
            print("[SmartFaceAnalyzer] WARNING: Haar Cascade unavailable or empty.")
            self.fallback_analyzer = None
        else:
            print("[SmartFaceAnalyzer] Fallback face detector initialized successfully.")

    def get(self, img):
        if img is None:
            return []
            
        if self.real_analyzer is not None:
            try:
                faces = self.real_analyzer.get(img)
                if faces and len(faces) > 0:
                    return faces
            except Exception as e:
                print(f"[SmartFaceAnalyzer] InsightFace get() error: {e}. Attempting fallback...")
                self.real_analyzer = None

        if self.fallback_analyzer is None:
            self.init_fallback()

        if self.fallback_analyzer is not None:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 and img.shape[2] == 3 else img
            detected = self.fallback_analyzer.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=3, minSize=(30, 30))
            
            class HaarFace:
                def __init__(self, bbox, embedding):
                    self.bbox = bbox
                    self.embedding = embedding
                    self.det_score = 0.90
                    
            faces = []
            for (x, y, w, h) in detected:
                face_crop = gray[y:y+h, x:x+w]
                resized = cv2.resize(face_crop, (16, 16), interpolation=cv2.INTER_AREA)
                sig_256 = resized.flatten().astype(np.float32) / 255.0
                mean = np.mean(sig_256)
                std = np.std(sig_256)
                if std > 1e-4:
                    sig_256 = (sig_256 - mean) / std
                else:
                    sig_256 = sig_256 - mean
                sig_512 = np.concatenate([sig_256, sig_256])
                faces.append(HaarFace(bbox=np.array([int(x), int(y), int(x+w), int(y+h)]), embedding=sig_512))
                
            return faces
            
        return []

face_analyzer = SmartFaceAnalyzer()
target_cache = {}

def resize_for_face_detection(img, max_size=640):
    if img is None:
        return None, 1.0
    h, w = img.shape[:2]
    if w > max_size or h > max_size:
        scale = max_size / float(max(w, h))
        new_w = int(w * scale)
        new_h = int(h * scale)
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA), scale
    return img, 1.0

def decode_base64_image(image_src):
    try:
        if "," in image_src:
            header, encoded = image_src.split(",", 1)
        else:
            encoded = image_src
        img_data = base64.b64decode(encoded)
        nparr = np.frombuffer(img_data, np.uint8)
        return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    except Exception as e:
        print(f"Error decoding base64 image: {e}")
        return None

def get_target_embedding(target):
    name = target.get("name", "Unknown")
    image_src = target.get("imageSrc", "")
    if not image_src:
        return None
    
    if image_src in target_cache:
        return target_cache[image_src]
    
    img = decode_base64_image(image_src)
    if img is None:
        return None
        
    resized_img, _ = resize_for_face_detection(img)
    faces = face_analyzer.get(resized_img)

    if not faces and resized_img is not None and len(resized_img.shape) == 3:
        lab = cv2.cvtColor(resized_img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        cl = clahe.apply(l)
        enhanced_lab = cv2.merge((cl, a, b))
        enhanced_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
        faces = face_analyzer.get(enhanced_bgr)

    if not faces and resized_img is not None:
        h, w = resized_img.shape[:2]
        fx, fy, fw, fh = int(w * 0.15), int(h * 0.10), int(w * 0.70), int(h * 0.80)
        gray = cv2.cvtColor(resized_img, cv2.COLOR_BGR2GRAY) if len(resized_img.shape) == 3 else resized_img
        face_crop = gray[fy:fy+fh, fx:fx+fw]
        if face_crop.size > 0:
            resized = cv2.resize(face_crop, (16, 16), interpolation=cv2.INTER_AREA)
            sig_256 = resized.flatten().astype(np.float32) / 255.0
            mean = np.mean(sig_256)
            std = np.std(sig_256)
            if std > 1e-4:
                sig_256 = (sig_256 - mean) / std
            else:
                sig_256 = sig_256 - mean
            sig_512 = np.concatenate([sig_256, sig_256])
            class FallbackFace:
                def __init__(self):
                    self.bbox = np.array([fx, fy, fx+fw, fy+fh])
                    self.embedding = sig_512
            faces = [FallbackFace()]
                
    if not faces:
        return None
        
    largest_face = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    embedding = largest_face.embedding
    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm
        
    target_cache[image_src] = embedding
    print(f"[Face Ingest] ✅ Cached embedding for target: '{name}' (Vector Dim: {len(embedding)})")
    return embedding

# =========================================================================
# TEMPORAL OBJECT TRACKER & STATE MACHINE (Person & Face)
# =========================================================================
class TemporalTracker:
    def __init__(self):
        self.tracks = {}  # track_id -> dict
        self.next_track_id = 1
        self.plate_history = collections.deque(maxlen=PLATE_WINDOW_M)
        self.lock = threading.Lock()

    def calculate_iou(self, boxA, boxB):
        xA = max(boxA[0], boxB[0])
        yA = max(boxA[1], boxB[1])
        xB = min(boxA[2], boxB[2])
        yB = min(boxA[3], boxB[3])
        interArea = max(0, xB - xA) * max(0, yB - yA)
        boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
        boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
        iou = interArea / float(boxAArea + boxBArea - interArea + 1e-6)
        return iou

    def update_face_tracks(self, frame_id, detected_matches):
        """
        Updates sliding-window history for detected face matches.
        Uses greedy bipartite spatial IoU association to track multiple faces independently.
        Enforces:
          - N out of M confirmation to mark CONFIRMED_PRESENT
          - K consecutive absent frames to mark CONFIRMED_ABSENT
        """
        with self.lock:
            confirmed_matches = []

            # 1. Build spatial overlap (IoU) candidates between existing tracks and new detections
            candidates = []
            for det_idx, det in enumerate(detected_matches):
                bbox = det["bbox"]
                det_name = det["name"]

                for t_id, track in self.tracks.items():
                    iou = self.calculate_iou(bbox, track["last_bbox"])
                    name_boost = 0.05 if (track["name"] == det_name or track["name"] == "UNAUTHORIZED PERSON") else 0.0
                    score = iou + name_boost

                    if iou >= 0.15:
                        candidates.append((score, iou, t_id, det_idx))

            # 2. Sort candidate assignments by descending score/IoU
            candidates.sort(key=lambda x: x[0], reverse=True)

            claimed_tracks = set()
            claimed_dets = set()
            assignments = {}

            for score, iou, t_id, det_idx in candidates:
                if t_id not in claimed_tracks and det_idx not in claimed_dets:
                    claimed_tracks.add(t_id)
                    claimed_dets.add(det_idx)
                    assignments[det_idx] = t_id

            # 3. Process matched detections and create new tracks for unmatched detections
            updated_track_ids = set()

            for det_idx, det in enumerate(detected_matches):
                bbox = det["bbox"]
                name = det["name"]
                conf = det["confidence"]

                if det_idx in assignments:
                    best_track_id = assignments[det_idx]
                else:
                    best_track_id = f"TRACK-{self.next_track_id:03d}"
                    self.next_track_id += 1
                    self.tracks[best_track_id] = {
                        "track_id": best_track_id,
                        "name": name,
                        "last_bbox": bbox,
                        "window": collections.deque(maxlen=PERSON_WINDOW_M),
                        "absent_count": 0,
                        "status": "TENTATIVE"
                    }

                track = self.tracks[best_track_id]
                track["last_bbox"] = bbox
                if name != "UNAUTHORIZED PERSON" or track["name"] == "UNAUTHORIZED PERSON":
                    track["name"] = name
                track["window"].append(True)
                track["absent_count"] = 0
                updated_track_ids.add(best_track_id)

                hits = sum(track["window"])
                track["status"] = "CONFIRMED_PRESENT" if hits >= PERSON_CONFIRM_N else "DETECTED"
                confirmed_matches.append({
                    "name": track["name"],
                    "confidence": conf,
                    "bbox": bbox,
                    "track_id": best_track_id,
                    "status": track["status"]
                })

            # 4. Update absent tracks that received no detection in this frame
            for t_id, track in list(self.tracks.items()):
                if t_id not in updated_track_ids:
                    track["window"].append(False)
                    track["absent_count"] += 1
                    if track["absent_count"] >= PERSON_ABSENT_K:
                        track["status"] = "CONFIRMED_ABSENT"
                        del self.tracks[t_id]

            return confirmed_matches

    def add_plate_observation(self, raw_text, norm_text, conf, bbox, timestamp):
        with self.lock:
            self.plate_history.append({
                "raw": raw_text,
                "norm": norm_text,
                "conf": conf,
                "bbox": bbox,
                "timestamp": timestamp
            })

    def get_plate_consensus(self):
        with self.lock:
            if not self.plate_history:
                return None

            counts = collections.defaultdict(int)
            total_conf = collections.defaultdict(float)
            latest_bbox = {}
            raw_map = {}

            for item in self.plate_history:
                norm = item["norm"]
                counts[norm] += 1
                total_conf[norm] += item["conf"]
                latest_bbox[norm] = item["bbox"]
                raw_map[norm] = item["raw"]

            best_norm = max(counts, key=lambda k: (counts[k], total_conf[k]))
            freq = counts[best_norm]
            avg_conf = total_conf[best_norm] / freq

            if avg_conf >= PLATE_MIN_CONF:
                return {
                    "text": best_norm,
                    "raw_text": raw_map[best_norm],
                    "confidence": round(avg_conf, 3),
                    "bbox": latest_bbox[best_norm],
                    "consensus_count": freq
                }
            return None

tracker = TemporalTracker()

# =========================================================================
# LICENSE PLATE ANPR PIPELINE & PREPROCESSING
# =========================================================================
def locate_license_plate_candidates(frame):
    if frame is None:
        return []
        
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    blur = cv2.bilateralFilter(gray, 9, 75, 75)
    sobel_x = cv2.Sobel(blur, cv2.CV_8U, 1, 0, ksize=3)
    _, thresh = cv2.threshold(sobel_x, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 5))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    candidates = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < 40 or ch < 12 or cw > w * 0.95 or ch > h * 0.95:
            continue
        aspect_ratio = float(cw) / float(ch)
        area = cw * ch
        if 1.8 <= aspect_ratio <= 6.5 and 450 <= area <= (w * h * 0.5):
            pad_x = int(cw * 0.08)
            pad_y = int(ch * 0.15)
            x1 = max(0, x - pad_x)
            y1 = max(0, y - pad_y)
            x2 = min(w, x + cw + pad_x)
            y2 = min(h, y + ch + pad_y)
            crop = frame[y1:y2, x1:x2]
            if crop.size > 0:
                candidates.append({
                    "bbox": [x1, y1, x2, y2],
                    "crop": crop
                })
    return candidates

def preprocess_plate_crop(crop):
    """Enhances plate crop before feeding to EasyOCR."""
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    if h < 10 or w < 25:
        return None

    target_h = 60
    scale = target_h / float(h)
    target_w = max(120, int(w * scale))

    resized = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY) if len(resized.shape) == 3 else resized

    # Contrast adjustment (CLAHE)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    denoised = cv2.bilateralFilter(enhanced, 7, 50, 50)
    return denoised

def normalize_indian_plate(text):
    """Positional character normalization for Indian plate formats (e.g. DL 01 AB 1234)."""
    cleaned = re.sub(r'[^A-Z0-9]', '', text.upper())
    if len(cleaned) < 4:
        return cleaned

    num_to_char = {'0': 'O', '1': 'I', '8': 'B', '5': 'S', '2': 'Z', '6': 'G', '4': 'A'}
    char_to_num = {'O': '0', 'Q': '0', 'I': '1', 'L': '1', 'B': '8', 'S': '5', 'Z': '2', 'G': '6', 'A': '4', 'T': '7'}

    pattern = re.compile(r'^([A-Z0-9]{2})([A-Z0-9]{1,2})([A-Z0-9]{1,2})([A-Z0-9]{4})$')
    match = pattern.match(cleaned)
    if match:
        state_p, dist_p, series_p, num_p = match.groups()
        s_clean = ''.join(num_to_char.get(c, c) for c in state_p)
        d_clean = ''.join(char_to_num.get(c, c) for c in dist_p)
        se_clean = ''.join(num_to_char.get(c, c) for c in series_p)
        n_clean = ''.join(char_to_num.get(c, c) for c in num_p)
        return f"{s_clean}{d_clean}{se_clean}{n_clean}"

    return cleaned

def save_debug_annotated_frame(frame, face_matches, plate_results, frame_id, timestamp):
    if not SAVE_DEBUG_FRAMES or frame is None:
        return
    try:
        os.makedirs("debug_frames", exist_ok=True)
        annotated = frame.copy()

        for m in face_matches:
            bbox = m.get("bbox", [])
            if len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                label = f"{m.get('name')} ({m.get('confidence', 0):.2f}) [{m.get('track_id', 'TRK')}]"
                cv2.putText(annotated, label, (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        for p in plate_results:
            bbox = p.get("bbox", [])
            if len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (255, 0, 0), 2)
                label = f"PLATE: {p.get('text')} ({p.get('confidence', 0):.2f})"
                cv2.putText(annotated, label, (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)

        cv2.putText(annotated, f"FRAME: #{frame_id} | TS: {timestamp:.2f}", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        cv2.imwrite("debug_frames/latest_debug.jpg", annotated)
    except Exception as e:
        print(f"[Debug Frame] Save exception: {e}")

# =========================================================================
# ENDPOINTS
# =========================================================================
@app.get("/")
def home():
    is_fallback = face_analyzer.real_analyzer is None
    status_mode = "Haar Cascade Fallback Mode" if is_fallback else "InsightFace High-Precision Mode"
    return {
        "status": "ANPR & Facial Recognition Backend Running",
        "mode": status_mode,
        "debug_mode": DEBUG_MODE
    }

@app.get("/api/debug-frame")
def get_debug_frame():
    filepath = "debug_frames/latest_debug.jpg"
    if os.path.exists(filepath):
        return FileResponse(filepath, media_type="image/jpeg")
    return {"error": "No debug frame available yet."}

@app.post("/api/scan-face")
def scan_face(
    file: UploadFile = File(...),
    targets: str = Form(...),
    frame_id: int = Form(0),
    timestamp: float = Form(0.0)
):
    req_ts = timestamp if timestamp > 0 else time.time()
    try:
        contents = file.file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return {"matches": [], "frame_id": frame_id, "timestamp": req_ts}
            
        try:
            target_list = json.loads(targets)
        except Exception:
            target_list = []
            
        valid_targets = []
        for target in target_list:
            emb = get_target_embedding(target)
            if emb is not None:
                valid_targets.append((target.get("name", "Unknown"), emb))
                
        # Process face detection regardless of whether target_list is empty
        processed_frame, scale = resize_for_face_detection(frame, max_size=640)
        faces = face_analyzer.get(processed_frame)
        
        raw_matches = []
        is_fallback = face_analyzer.real_analyzer is None
        
        person_conf_log = 0.0
        person_bbox_log = []

        for face in faces:
            det_score = getattr(face, 'det_score', 0.88)
            if det_score is not None and det_score < FACE_DET_SCORE_THRESHOLD:
                continue

            raw_bbox = face.bbox.tolist() if hasattr(face.bbox, 'tolist') else list(face.bbox)
            orig_bbox = [
                int(raw_bbox[0] / scale),
                int(raw_bbox[1] / scale),
                int(raw_bbox[2] / scale),
                int(raw_bbox[3] / scale)
            ]

            embedding = getattr(face, 'embedding', None)
            top_name = None
            top_sim = 0.0

            if embedding is not None and valid_targets:
                norm = np.linalg.norm(embedding)
                if norm > 0:
                    embedding = embedding / norm

                person_scores = {}
                for name, target_emb in valid_targets:
                    similarity = float(np.dot(embedding, target_emb))
                    person_scores[name] = max(person_scores.get(name, -1.0), similarity)

                scores = sorted([(name, sim) for name, sim in person_scores.items()], key=lambda x: x[1], reverse=True)

                if scores:
                    candidate_name, candidate_sim = scores[0]
                    threshold = FACE_MATCH_SIM_THRESHOLD
                    
                    margin_valid = True
                    if len(scores) > 1 and not is_fallback:
                        second_name, second_sim = scores[1]
                        if second_sim > 0.20 and (candidate_sim - second_sim) < 0.03:
                            margin_valid = False

                    if candidate_sim >= threshold and margin_valid:
                        top_name = candidate_name
                        top_sim = candidate_sim

            if top_name is not None:
                person_conf_log = top_sim
                person_bbox_log = orig_bbox
                raw_matches.append({
                    "name": top_name,
                    "confidence": float(top_sim),
                    "bbox": orig_bbox
                })
            else:
                # UN-ENROLLED PERSON / INTRUDER DETECTED
                unauth_conf = float(det_score if det_score is not None else 0.88)
                person_conf_log = unauth_conf
                person_bbox_log = orig_bbox
                raw_matches.append({
                    "name": "UNAUTHORIZED PERSON",
                    "confidence": unauth_conf,
                    "bbox": orig_bbox
                })

        # Apply Temporal Confirmation Tracker
        confirmed_matches = tracker.update_face_tracks(frame_id, raw_matches)

        final_decision = "CONFIRMED_MATCH" if confirmed_matches else ("RAW_MATCH" if raw_matches else "NO_FACE")
        track_id_log = confirmed_matches[0]["track_id"] if confirmed_matches else "NONE"

        if DEBUG_MODE:
            print(f"[DEBUG_LOG] FRAME_ID={frame_id} | TIMESTAMP={req_ts:.3f} | PERSON_CONF={person_conf_log:.2f} | PERSON_BBOX={person_bbox_log} | TRACK_ID={track_id_log} | FINAL_DECISION={final_decision}")

        save_debug_annotated_frame(frame, confirmed_matches, [], frame_id, req_ts)

        return {
            "matches": confirmed_matches,
            "frame_id": frame_id,
            "timestamp": req_ts
        }
    except Exception as e:
        print(f"[Backend Face API] Error: {e}")
        return {"matches": [], "frame_id": frame_id, "timestamp": req_ts}

@app.post("/api/scan-plate")
def scan_plate(
    file: UploadFile = File(...),
    frame_id: int = Form(0),
    timestamp: float = Form(0.0)
):
    req_ts = timestamp if timestamp > 0 else time.time()
    try:
        contents = file.file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return {"results": [], "frame_id": frame_id, "timestamp": req_ts}

        h, w, _ = frame.shape
        candidates = locate_license_plate_candidates(frame)
        
        ocr_raw_log = ""
        ocr_norm_log = ""
        ocr_conf_log = 0.0
        plate_bbox_log = []

        for cand in candidates:
            crop = cand["crop"]
            bbox = cand["bbox"]
            
            preprocessed = preprocess_plate_crop(crop)
            if preprocessed is None:
                continue

            ocr_results = reader.readtext(preprocessed)
            for (local_bbox, text, prob) in ocr_results:
                if prob >= 0.25:
                    norm_text = normalize_indian_plate(text)
                    if len(norm_text) >= 3:
                        ocr_raw_log = text
                        ocr_norm_log = norm_text
                        ocr_conf_log = float(prob)
                        plate_bbox_log = bbox
                        tracker.add_plate_observation(text, norm_text, float(prob), bbox, req_ts)

        consensus_plate = tracker.get_plate_consensus()
        results = [consensus_plate] if consensus_plate else []
        final_decision = "CONFIRMED_PLATE" if results else "NO_PLATE"

        if DEBUG_MODE and ocr_norm_log:
            print(f"[DEBUG_LOG] FRAME_ID={frame_id} | TIMESTAMP={req_ts:.3f} | PLATE_CONF={ocr_conf_log:.2f} | PLATE_BBOX={plate_bbox_log} | OCR_RAW='{ocr_raw_log}' | OCR_NORM='{ocr_norm_log}' | OCR_CONF={ocr_conf_log:.2f} | FINAL_DECISION={final_decision}")

        save_debug_annotated_frame(frame, [], results, frame_id, req_ts)

        return {
            "results": results,
            "frame_id": frame_id,
            "timestamp": req_ts
        }
    except Exception as e:
        print(f"[Backend Plate API] Error: {e}")
        return {"results": [], "frame_id": frame_id, "timestamp": req_ts}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)