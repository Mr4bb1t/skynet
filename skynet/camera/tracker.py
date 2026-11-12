# ==============================================================================
# RASTREAMENTO AUTOMÁTICO DE PESSOAS (AUTO-FOLLOW) COM PTZ POR PASSOS
#
# A câmera só aceita ~1 passo fixo de motor por segundo (~8° por passo), então
# este módulo NÃO tenta servomecanismo contínuo: ele detecta a pessoa, mede o
# desvio em relação ao centro do quadro e dá UM passo naquele eixo, esperando o
# vídeo estabilizar antes de decidir de novo (~1,4 s por passo).
#
# Detectores disponíveis:
#   - "hog"    : OpenCV HOG (pessoa de corpo inteiro) + cascatas de corpo/rosto
#   - "ssd"    : MobileNet-SSD via cv2.dnn (precisa dos arquivos do modelo)
#   - "motion" : fallback sem OpenCV (diferença de frames, heurística de forma)
#
# Requer: python3-opencv (apt) no TV Box. Sem OpenCV o modo "motion" continua
# funcionando usando PIL.
# ==============================================================================
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import config

try:  # OpenCV é opcional: sem ele o rastreador cai no modo "motion"
    import cv2
    import numpy as np
    _CV2 = True
except Exception:  # pragma: no cover - depende do ambiente
    cv2 = None
    np = None
    _CV2 = False

try:
    from PIL import Image, ImageChops
    _PIL = True
except Exception:  # pragma: no cover
    _PIL = False

Box = Tuple[int, int, int, int]
Detection = Tuple[int, int, int, int, float]  # x, y, w, h, score


# ------------------------------------------------------------------------------
# Utilidades geométricas
# ------------------------------------------------------------------------------
def _iou(a: Box, b: Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / float(union or 1)


def _nms(dets: List[Detection], thr: float = 0.45) -> List[Detection]:
    dets = sorted(dets, key=lambda d: d[4], reverse=True)
    kept: List[Detection] = []
    for d in dets:
        if all(_iou(d[:4], k[:4]) < thr for k in kept):
            kept.append(d)
    return kept


# ------------------------------------------------------------------------------
# Detectores
# ------------------------------------------------------------------------------
class BaseDetector:
    name = "base"

    def detect(self, frame) -> List[Detection]:
        raise NotImplementedError


class HogPersonDetector(BaseDetector):
    """HOG (corpo inteiro) + cascatas de corpo superior/rosto como reforço."""

    name = "hog"

    def __init__(self,
                 max_width: int = 480,
                 hit_threshold: float = 0.0,
                 scale: float = 1.05,
                 use_upper_body: bool = True,
                 use_face: bool = True):
        if not _CV2:
            raise RuntimeError("OpenCV não instalado")
        self.max_width = max_width
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.hit_threshold = hit_threshold
        self.scale = scale

        casc_dir = ""
        try:
            casc_dir = cv2.data.haarcascades
        except Exception:
            pass

        self.upper = None
        self.face = None
        if use_upper_body and casc_dir:
            path = os.path.join(casc_dir, "haarcascade_upperbody.xml")
            if os.path.exists(path):
                self.upper = cv2.CascadeClassifier(path)
        if use_face and casc_dir:
            path = os.path.join(casc_dir, "haarcascade_frontalface_default.xml")
            if os.path.exists(path):
                self.face = cv2.CascadeClassifier(path)

    def detect(self, frame) -> List[Detection]:
        h, w = frame.shape[:2]
        if w > self.max_width:
            ratio = self.max_width / float(w)
            small = cv2.resize(frame, (self.max_width, int(h * ratio)))
        else:
            ratio = 1.0
            small = frame

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        sh, sw = gray.shape[:2]
        dets: List[Detection] = []

        # 1) HOG: pessoas de corpo inteiro (mais longe)
        try:
            rects, weights = self.hog.detectMultiScale(
                gray,
                winStride=(8, 8),
                padding=(8, 8),
                scale=self.scale,
                hitThreshold=self.hit_threshold,
            )
            for (x, y, bw, bh), wt in zip(rects, weights):
                if bw < sw * 0.08 or bh < sh * 0.20:
                    continue  # pequeno demais para ser útil
                score = max(0.0, min(1.0, float(wt) / 1.5))
                dets.append((int(x), int(y), int(bw), int(bh), score))
        except Exception:
            pass

        # 2) Corpo superior (pessoa perto da câmera / atrás de móveis)
        if self.upper is not None:
            try:
                rects = self.upper.detectMultiScale(
                    gray, scaleFactor=1.08, minNeighbors=4,
                    minSize=(int(sw * 0.10), int(sh * 0.16)),
                )
                for (x, y, bw, bh) in rects:
                    # estima corpo inteiro a partir do tronco
                    y2 = min(sh, y + int(bh * 1.7))
                    conf = min(1.0, 0.45 + bw / float(sw))
                    dets.append((int(x), int(y), int(bw), int(y2 - y), conf))
            except Exception:
                pass

        # 3) Rosto (pessoa muito perto; evita "perder" o alvo quando só o rosto aparece)
        if self.face is not None:
            try:
                rects = self.face.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5,
                    minSize=(int(sw * 0.05), int(sh * 0.09)),
                )
                for (x, y, bw, bh) in rects:
                    y2 = min(sh, y + int(bh * 5.0))
                    x2 = max(0, x - int(bw * 0.8))
                    w3 = min(sw - x2, int(bw * 2.6))
                    conf = min(1.0, 0.5 + bw / float(sw))
                    dets.append((int(x2), int(y), int(w3), int(y2 - y), conf))
            except Exception:
                pass

        dets = _nms(dets, 0.35)
        inv = 1.0 / ratio if ratio else 1.0
        return [(int(x * inv), int(y * inv), int(bw * inv), int(bh * inv), s)
                for x, y, bw, bh, s in dets]


class SSDDetector(BaseDetector):
    """MobileNet-SSD (Caffe) — pessoa = classe 15."""

    name = "ssd"

    def __init__(self, prototxt: str, model: str,
                 confidence: float = 0.45, size: Tuple[int, int] = (300, 300)):
        if not _CV2:
            raise RuntimeError("OpenCV não instalado")
        if not os.path.exists(prototxt) or not os.path.exists(model):
            raise FileNotFoundError(f"Modelo SSD ausente: {prototxt} / {model}")
        self.net = cv2.dnn.readNetFromCaffe(prototxt, model)
        self.confidence = confidence
        self.size = size

    def detect(self, frame) -> List[Detection]:
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            cv2.resize(frame, self.size), 0.007843, self.size, 127.5
        )
        self.net.setInput(blob)
        out = self.net.forward()
        dets: List[Detection] = []
        for i in range(out.shape[2]):
            conf = float(out[0, 0, i, 2])
            if conf < self.confidence:
                continue
            if int(out[0, 0, i, 1]) != 15:  # 15 = person no VOC
                continue
            x1 = max(0, int(out[0, 0, i, 3] * w))
            y1 = max(0, int(out[0, 0, i, 4] * h))
            x2 = min(w, int(out[0, 0, i, 5] * w))
            y2 = min(h, int(out[0, 0, i, 6] * h))
            if x2 <= x1 or y2 <= y1:
                continue
            dets.append((x1, y1, x2 - x1, y2 - y1, conf))
        return _nms(dets, 0.4)


class MotionDetector(BaseDetector):
    """Fallback: diferença de frames + heurística de forma (sem modelo)."""

    name = "motion"

    def __init__(self, threshold: int = 22, min_area_ratio: float = 0.01,
                 max_width: int = 320):
        self.threshold = threshold
        self.min_area_ratio = min_area_ratio
        self.max_width = max_width
        self._prev = None

    def detect(self, frame) -> List[Detection]:
        h, w = frame.shape[:2]
        if _CV2:
            if w > self.max_width:
                ratio = self.max_width / float(w)
                small = cv2.resize(frame, (self.max_width, int(h * ratio)))
            else:
                ratio = 1.0
                small = frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            if self._prev is None:
                self._prev = gray
                return []
            diff = cv2.absdiff(self._prev, gray)
            self._prev = gray
            _, mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
            mask = cv2.dilate(mask, None, iterations=2)
            mask = cv2.erode(mask, None, iterations=1)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            sh, sw = gray.shape[:2]
            dets: List[Detection] = []
            for c in contours:
                x, y, bw, bh = cv2.boundingRect(c)
                area_ratio = (bw * bh) / float(sw * sh)
                if area_ratio < self.min_area_ratio:
                    continue
                aspect = bw / float(bh or 1)
                if not 0.15 <= aspect <= 1.4:  # forma "vertical" de gente
                    continue
                if bh < sh * 0.15:
                    continue
                conf = min(1.0, 0.3 + area_ratio * 6)
                dets.append((int(x / ratio), int(y / ratio),
                             int(bw / ratio), int(bh / ratio), conf))
            return _nms(dets, 0.4)

        # ---- caminho sem OpenCV: PIL puro ----
        if not _PIL:
            return []
        img = Image.open(frame).convert("L")
        if img.width > self.max_width:
            img = img.resize((self.max_width,
                              int(img.height * self.max_width / img.width)))
        if self._prev is None:
            self._prev = img
            return []
        diff = ImageChops.difference(self._prev, img).point(
            lambda p: 255 if p > self.threshold else 0
        )
        self._prev = img
        bbox = diff.getbbox()
        if not bbox:
            return []
        x1, y1, x2, y2 = bbox
        bw, bh = x2 - x1, y2 - y1
        area_ratio = (bw * bh) / float(img.width * img.height)
        if area_ratio < self.min_area_ratio or bh < img.height * 0.15:
            return []
        inv = 640.0 / img.width
        return [(int(x1 * inv), int(y1 * inv), int(bw * inv), int(bh * inv), 0.5)]


def build_detector(name: Optional[str] = None) -> BaseDetector:
    """Cria o detector pedido em config (auto|hog|ssd|motion)."""
    name = (name or config.TRACK_DETECTOR or "auto").lower()
    if name == "auto":
        name = "hog" if _CV2 else "motion"
    if name == "hog":
        return HogPersonDetector()
    if name == "ssd":
        return SSDDetector(config.TRACK_SSD_PROTOTXT, config.TRACK_SSD_MODEL)
    return MotionDetector()


# ------------------------------------------------------------------------------
# Rastreador
# ------------------------------------------------------------------------------
class PersonTracker:
    """Mantém a maior pessoa detectada perto do centro, dando passos de motor."""

    def __init__(self,
                 ptz,
                 vision,
                 detector: Optional[BaseDetector] = None,
                 pan_deadzone: Optional[float] = None,
                 tilt_deadzone: Optional[float] = None,
                 hysteresis: Optional[float] = None,
                 settle_sec: Optional[float] = None,
                 save_annotated: bool = False,
                 verbose: bool = True):
        self.ptz = ptz
        self.vision = vision
        self.detector = detector or build_detector()
        self.pan_deadzone = config.TRACK_PAN_DEADZONE if pan_deadzone is None else pan_deadzone
        self.tilt_deadzone = config.TRACK_TILT_DEADZONE if tilt_deadzone is None else tilt_deadzone
        self.hysteresis = config.TRACK_HYSTERESIS if hysteresis is None else hysteresis
        self.settle_sec = config.TRACK_SETTLE_SEC if settle_sec is None else settle_sec
        self.save_annotated = save_annotated
        self.verbose = verbose

        self.stats: Dict[str, Any] = {
            "steps": 0,
            "pan_steps": 0,
            "tilt_steps": 0,
            "frames": 0,
            "detections": 0,
            "lost_events": 0,
            "seconds": 0.0,
            "last_target": None,
            "reason": "not_started",
        }
        self._armed_pan = False
        self._armed_tilt = False
        self._ann_counter = 0

    # -- helpers -------------------------------------------------------------
    def _log(self, msg: str):
        if self.verbose:
            print(f"[Tracker] {msg}", flush=True)

    def _decode(self, jpeg: Optional[bytes]):
        if not jpeg or not _CV2:
            return None
        arr = np.frombuffer(jpeg, np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame

    @staticmethod
    def _pick_target(dets: List[Detection], w: int, h: int) -> Optional[Detection]:
        """Maior pessoa, penalizando quem está muito longe do centro."""
        best, best_score = None, -1.0
        for (x, y, bw, bh, conf) in dets:
            cx = (x + bw / 2.0) / w
            cy = (y + bh / 2.0) / h
            dist = ((cx - 0.5) ** 2 + (cy - 0.5) ** 2) ** 0.5
            area = (bw * bh) / float(w * h)
            score = area * (1.0 - 0.35 * min(1.0, dist)) * (0.5 + conf)
            if score > best_score:
                best, best_score = (x, y, bw, bh, conf), score
        return best

    def _annotate(self, frame, dets, target, cx, cy):
        if not (_CV2 and self.save_annotated and frame is not None):
            return
        try:
            os.makedirs(config.TRACK_ANNOTATED_DIR, exist_ok=True)
            h, w = frame.shape[:2]
            cv2.line(frame, (int(w * (0.5 - self.pan_deadzone)), 0),
                     (int(w * (0.5 - self.pan_deadzone)), h), (255, 0, 0), 1)
            cv2.line(frame, (int(w * (0.5 + self.pan_deadzone)), 0),
                     (int(w * (0.5 + self.pan_deadzone)), h), (255, 0, 0), 1)
            cv2.line(frame, (0, int(h * (0.5 - self.tilt_deadzone))),
                     (w, int(h * (0.5 - self.tilt_deadzone))), (0, 255, 255), 1)
            cv2.line(frame, (0, int(h * (0.5 + self.tilt_deadzone))),
                     (w, int(h * (0.5 + self.tilt_deadzone))), (0, 255, 255), 1)
            for (x, y, bw, bh, conf) in dets:
                cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 1)
            if target:
                x, y, bw, bh, _ = target
                cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 0, 255), 2)
            if cx is not None:
                cv2.circle(frame, (int(cx), int(cy)), 4, (0, 0, 255), -1)
            path = os.path.join(config.TRACK_ANNOTATED_DIR,
                                f"track_{self._ann_counter:05d}.jpg")
            cv2.imwrite(path, frame)
            self._ann_counter += 1
        except Exception:
            pass

    # -- loop ----------------------------------------------------------------
    def run(self, duration: Optional[float] = None,
            max_steps: Optional[int] = None) -> Dict[str, Any]:
        """
        Fica rastreando pessoas até `duration` segundos (ou Ctrl+C).

        :param duration: limite de tempo em segundos (None = infinito)
        :param max_steps: limite de passos de motor (segurança)
        """
        max_steps = config.TRACK_MAX_STEPS if max_steps is None else max_steps
        start = time.monotonic()
        last_seen: Optional[float] = None
        next_move_at = 0.0
        self.stats.update(reason="timeout")

        self._log(f"detector={self.detector.name} "
                  f"deadzone pan={self.pan_deadzone:.0%} tilt={self.tilt_deadzone:.0%} "
                  f"passo={config.PTZ_STEP_INTERVAL_SEC:.2f}s")

        try:
            while duration is None or (time.monotonic() - start) < duration:
                now = time.monotonic()

                # Espera o vídeo estabilizar depois do último passo
                wait = next_move_at - now
                if wait > 0:
                    time.sleep(min(wait, 0.2))
                    continue

                jpeg = self.vision.get_latest_frame_bytes()
                frame = self._decode(jpeg)
                if frame is None:
                    time.sleep(0.2)
                    continue

                self.stats["frames"] += 1
                h, w = frame.shape[:2]
                try:
                    dets = self.detector.detect(frame)
                except Exception as exc:
                    self._log(f"erro no detector: {exc}")
                    dets = []

                target = self._pick_target(dets, w, h) if dets else None
                cx = cy = None
                if target:
                    self.stats["detections"] += 1
                    last_seen = time.monotonic()
                    self.stats["last_target"] = target
                    x, y, bw, bh, _ = target
                    cx = (x + bw / 2.0) / w
                    cy = (y + bh / 2.0) / h
                else:
                    if last_seen and (time.monotonic() - last_seen) > config.TRACK_LOST_TIMEOUT:
                        self.stats["lost_events"] += 1
                        self._log("pessoa perdida — aguardando reaparecer")
                        last_seen = None
                        self._armed_pan = self._armed_tilt = False

                self._annotate(frame, dets, target, cx, cy)

                # ---- decisão de movimento ----
                axis = None
                direction = 0
                if target:
                    dx = cx - 0.5
                    dy = cy - 0.5

                    # histerese: arma só quando passa da zona morta + folga
                    if abs(dx) > self.pan_deadzone + self.hysteresis:
                        self._armed_pan = True
                    elif abs(dx) < self.pan_deadzone:
                        self._armed_pan = False

                    if abs(dy) > self.tilt_deadzone + self.hysteresis:
                        self._armed_tilt = True
                    elif abs(dy) < self.tilt_deadzone:
                        self._armed_tilt = False

                    ex = abs(dx) - self.pan_deadzone if self._armed_pan else -1.0
                    ey = abs(dy) - self.tilt_deadzone if self._armed_tilt else -1.0

                    if ex > 0 or ey > 0:
                        if ex >= ey:
                            axis, direction = "pan", (1 if dx > 0 else -1)
                        else:
                            axis, direction = "tilt", (-1 if dy > 0 else 1)

                    self._log(
                        f"alvo {bw}x{bh}px desvio=({dx:+.2f},{dy:+.2f}) "
                        f"passos={self.stats['steps']}"
                    )

                if axis and self.stats["steps"] < max_steps:
                    ok = self.ptz.move_steps(direction, axis)
                    self.stats["steps"] += 1
                    self.stats["pan_steps" if axis == "pan" else "tilt_steps"] += 1
                    if not ok:
                        self._log("falha ao enviar passo")
                    next_move_at = time.monotonic() + config.PTZ_STEP_INTERVAL_SEC + self.settle_sec
                else:
                    time.sleep(0.2)

            self.stats["reason"] = "duration"
        except KeyboardInterrupt:
            self.stats["reason"] = "interrupted"
            self._log("interrompido pelo usuário")

        self.stats["seconds"] = round(time.monotonic() - start, 2)
        try:
            self.ptz.stop()
        except Exception:
            pass
        return self.stats


def self_test() -> bool:
    """Teste rápido sem mover a câmera: valida decodificação e detecção."""
    from camera.vision import VisionModule

    vision = VisionModule()
    det = build_detector()
    jpeg = vision.get_latest_frame_bytes()
    if not jpeg:
        print("SEM_FRAME")
        return False
    tracker = PersonTracker(ptz=None, vision=vision, detector=det, verbose=False)
    frame = tracker._decode(jpeg)
    if frame is None:
        print("SEM_OPENCV (frame não decodificado)")
        return False
    t0 = time.monotonic()
    dets = det.detect(frame)
    dt = time.monotonic() - t0
    print(f"detector={det.name} frame={frame.shape[1]}x{frame.shape[0]} "
          f"tempo={dt*1000:.0f}ms detectados={len(dets)}")
    for d in dets[:5]:
        print(f"  caixa x={d[0]} y={d[1]} w={d[2]} h={d[3]} conf={d[4]:.2f}")
    return True


if __name__ == "__main__":
    self_test()
