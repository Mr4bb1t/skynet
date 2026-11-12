"""Memória local de pessoas com reconhecimento facial.

Cada foto capturada pela câmera é analisada localmente:

  * OpenCV Haar detecta os rostos (rápido no ARM);
  * dlib/face_recognition gera um embedding de 128 dimensões por rosto;
  * o embedding é comparado com os rostos já salvos no SQLite;
  * um JSON com as pessoas reconhecidas acompanha a imagem enviada à IA.

Nada de rosto sai para a nuvem: o banco e as fotos ficam no TV Box.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - opcional
    cv2 = None  # type: ignore

try:
    import face_recognition  # type: ignore
except Exception:  # pragma: no cover - opcional
    face_recognition = None  # type: ignore

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - opcional
    np = None  # type: ignore

LOG = logging.getLogger("vision.people")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DetectedFace:
    face_index: int
    bbox: list[int]  # x, y, w, h (OpenCV)
    location: tuple[int, int, int, int]  # top, right, bottom, left (dlib)
    encoding: Optional[Any] = None
    person_id: Optional[int] = None
    name: str = ""
    nickname: str = ""
    notes: str = ""
    distance: Optional[float] = None
    known: bool = False


@dataclass
class FaceAnalysis:
    faces: list[DetectedFace] = field(default_factory=list)
    public: dict[str, Any] = field(default_factory=dict)

    @property
    def json_text(self) -> str:
        return json.dumps(self.public, ensure_ascii=False)

    @classmethod
    def empty(cls, reason: str = "ok") -> "FaceAnalysis":
        return cls(
            faces=[],
            public={
                "people": [],
                "unknown_faces": 0,
                "faces": 0,
                "memory": reason,
            },
        )


class PersonMemory:
    """Banco SQLite + reconhecimento facial local."""

    def __init__(
        self,
        db_path: str,
        photo_dir: str,
        *,
        threshold: float = 0.6,
        min_face_size: int = 50,
        enabled: bool = True,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser())
        self.photo_dir = Path(photo_dir).expanduser()
        self.threshold = float(threshold)
        self.min_face_size = max(20, int(min_face_size))
        self.available = False
        self.reason = ""
        self._lock = threading.RLock()
        self._cascade = None
        self._alt_cascade = None

        if not enabled:
            self.reason = "disabled"
            return
        if cv2 is None or face_recognition is None or np is None:
            self.reason = "face_recognition/opencv indisponível"
            LOG.warning("Memória de pessoas desativada: %s", self.reason)
            return

        try:
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            alt_path = cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml"
            self._cascade = cv2.CascadeClassifier(cascade_path)
            self._alt_cascade = cv2.CascadeClassifier(alt_path)
            if self._cascade.empty() and self._alt_cascade.empty():
                raise RuntimeError("cascatas Haar não encontradas")
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self.photo_dir.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self.available = True
        except Exception as exc:  # pragma: no cover - depende do ambiente
            self.reason = str(exc)
            LOG.warning("Memória de pessoas desativada: %s", exc)

    # ------------------------------------------------------------------
    # Banco
    # ------------------------------------------------------------------
    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    photo_path TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE TABLE IF NOT EXISTS faces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL,
                    encoding TEXT NOT NULL,
                    photo_path TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_faces_person
                    ON faces(person_id);
                """
            )

    @staticmethod
    def _person_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "person_id": row["id"],
            "name": row["name"],
            "nickname": row["nickname"] or "",
            "notes": row["notes"] or "",
            "photo_path": row["photo_path"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_seen_at": row["last_seen_at"] or "",
        }

    def _known_faces(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT f.id AS face_id, f.encoding, p.*
                FROM faces f
                JOIN people p ON p.id = f.person_id
                """
            ).fetchall()
        known: list[dict[str, Any]] = []
        for row in rows:
            try:
                encoding = np.asarray(json.loads(row["encoding"]), dtype=np.float64)
            except Exception:
                continue
            known.append(
                {
                    "face_id": row["face_id"],
                    "encoding": encoding,
                    "person": self._person_dict(row),
                }
            )
        return known

    # ------------------------------------------------------------------
    # Análise de uma foto
    # ------------------------------------------------------------------
    def analyze_jpeg(self, jpeg_bytes: bytes) -> FaceAnalysis:
        if not self.available:
            return FaceAnalysis.empty(self.reason or "unavailable")
        if not jpeg_bytes:
            return FaceAnalysis.empty("invalid_image")
        try:
            buffer = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        except Exception as exc:
            LOG.warning("Falha ao decodificar JPEG para rostos: %s", exc)
            return FaceAnalysis.empty("invalid_image")
        if image is None:
            return FaceAnalysis.empty("invalid_image")

        # Limita o tamanho para manter a detecção rápida no ARM.
        height, width = image.shape[:2]
        scale = min(1.0, 960.0 / max(height, width))
        if scale < 1.0:
            image = cv2.resize(
                image, (max(1, int(width * scale)), max(1, int(height * scale)))
            )

        try:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            rects = self._cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(self.min_face_size, self.min_face_size),
            )
            if len(rects) == 0 and self._alt_cascade is not None:
                rects = self._alt_cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(self.min_face_size, self.min_face_size),
                )
            rects = list(rects)
            if not rects:
                return FaceAnalysis.empty("ok")

            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            locations = [
                (int(y), int(x + w), int(y + h), int(x)) for (x, y, w, h) in rects
            ]
            encodings = face_recognition.face_encodings(
                rgb, locations, num_jitters=0
            )
        except Exception as exc:
            LOG.warning("Falha na análise facial: %s", exc)
            return FaceAnalysis.empty("face_error")

        known = self._known_faces()
        faces: list[DetectedFace] = []
        matched_ids: set[int] = set()
        for index, (rect, location, encoding) in enumerate(
            zip(rects, locations, encodings)
        ):
            x, y, w, h = (int(v) for v in rect)
            face = DetectedFace(
                face_index=index,
                bbox=[x, y, w, h],
                location=location,
                encoding=encoding,
            )
            if known:
                distances = [
                    float(np.linalg.norm(encoding - item["encoding"]))
                    for item in known
                ]
                best = int(np.argmin(distances))
                best_distance = distances[best]
                face.distance = best_distance
                if best_distance <= self.threshold:
                    person = known[best]["person"]
                    face.person_id = int(person["person_id"])
                    face.name = person["name"]
                    face.nickname = person["nickname"]
                    face.notes = person["notes"]
                    face.known = True
                    matched_ids.add(face.person_id)
            faces.append(face)

        if matched_ids:
            now = _now_iso()
            with self._lock, self._db() as conn:
                conn.executemany(
                    "UPDATE people SET last_seen_at=? WHERE id=?",
                    [(now, person_id) for person_id in matched_ids],
                )

        public_people = []
        for face in faces:
            person = None
            if face.known:
                person = {
                    "person_id": face.person_id,
                    "name": face.name,
                    "nickname": face.nickname,
                    "notes": face.notes,
                }
            public_people.append(
                {
                    "face_index": face.face_index,
                    "known": face.known,
                    "distance": (
                        None if face.distance is None else round(face.distance, 3)
                    ),
                    "bbox": face.bbox,
                    "person": person,
                }
            )

        public = {
            "people": public_people,
            "unknown_faces": sum(1 for face in faces if not face.known),
            "faces": len(faces),
            "memory": "ok",
        }
        return FaceAnalysis(faces=faces, public=public)

    # ------------------------------------------------------------------
    # CRUD de pessoas
    # ------------------------------------------------------------------
    def remember(
        self,
        name: str,
        nickname: str = "",
        notes: str = "",
        face_index: int = 0,
        *,
        faces: list[DetectedFace],
        jpeg_bytes: Optional[bytes] = None,
    ) -> dict[str, Any]:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        name = (name or "").strip()
        if not name:
            return {"status": "error", "error": "nome vazio"}
        if not faces:
            return {
                "status": "error",
                "error": "nenhuma foto analisada; capture uma foto primeiro",
            }
        try:
            face_index = int(face_index)
        except (TypeError, ValueError):
            face_index = 0
        if face_index < 0 or face_index >= len(faces):
            return {
                "status": "error",
                "error": f"face_index inválido (0..{len(faces) - 1})",
            }
        face = faces[face_index]
        if face.encoding is None:
            return {"status": "error", "error": "rosto sem embedding"}

        now = _now_iso()
        with self._lock, self._db() as conn:
            person_id = face.person_id
            row = None
            if person_id is not None:
                row = conn.execute(
                    "SELECT * FROM people WHERE id=?", (person_id,)
                ).fetchone()
                if row is None:
                    person_id = None
            if row is None:
                row = conn.execute(
                    "SELECT * FROM people WHERE name = ? COLLATE NOCASE", (name,)
                ).fetchone()
                if row is not None:
                    person_id = int(row["id"])

            if person_id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO people
                        (name, nickname, notes, photo_path, created_at, updated_at)
                    VALUES (?, ?, ?, '', ?, ?)
                    """,
                    (name, nickname.strip(), notes.strip(), now, now),
                )
                person_id = int(cursor.lastrowid)
            else:
                existing_notes = (row["notes"] or "").strip()
                merged_notes = existing_notes
                if notes.strip():
                    if notes.strip() not in existing_notes:
                        merged_notes = (
                            f"{existing_notes} | {notes.strip()}"
                            if existing_notes
                            else notes.strip()
                        )
                conn.execute(
                    """
                    UPDATE people
                       SET name=?, nickname=?, notes=?, updated_at=?
                     WHERE id=?
                    """,
                    (
                        name,
                        nickname.strip() or (row["nickname"] or ""),
                        merged_notes,
                        now,
                        person_id,
                    ),
                )

            # Evita duplicar o mesmo rosto na mesma pessoa.
            add_encoding = True
            for existing in conn.execute(
                "SELECT encoding FROM faces WHERE person_id=?", (person_id,)
            ).fetchall():
                try:
                    known_encoding = np.asarray(
                        json.loads(existing["encoding"]), dtype=np.float64
                    )
                    if float(np.linalg.norm(known_encoding - face.encoding)) < 0.4:
                        add_encoding = False
                        break
                except Exception:
                    continue

            photo_path = ""
            if jpeg_bytes:
                photo_path = str(
                    self.photo_dir
                    / f"person_{person_id}_{int(time.time() * 1000)}.jpg"
                )
                try:
                    Path(photo_path).write_bytes(jpeg_bytes)
                except OSError as exc:
                    LOG.warning("Não foi possível salvar foto da pessoa: %s", exc)
                    photo_path = ""

            if add_encoding:
                conn.execute(
                    """
                    INSERT INTO faces (person_id, encoding, photo_path, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        person_id,
                        json.dumps(face.encoding.tolist()),
                        photo_path,
                        now,
                    ),
                )
            if photo_path:
                conn.execute(
                    "UPDATE people SET photo_path=? WHERE id=?",
                    (photo_path, person_id),
                )

            row = conn.execute(
                "SELECT * FROM people WHERE id=?", (person_id,)
            ).fetchone()

        result = self._person_dict(row)
        result["face_index"] = face_index
        return {"status": "ok", "person": result, "face_index": face_index}

    def recall(self, query: str) -> dict[str, Any]:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        query = (query or "").strip()
        if not query:
            return self.list_people()
        pattern = f"%{query}%"
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM people
                 WHERE name LIKE ? OR nickname LIKE ? OR notes LIKE ?
                 ORDER BY name COLLATE NOCASE
                """,
                (pattern, pattern, pattern),
            ).fetchall()
        return {"status": "ok", "people": [self._person_dict(row) for row in rows]}

    def list_people(self) -> dict[str, Any]:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM people ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return {"status": "ok", "people": [self._person_dict(row) for row in rows]}

    def forget(self, name: str) -> dict[str, Any]:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        name = (name or "").strip()
        if not name:
            return {"status": "error", "error": "nome vazio"}
        with self._lock, self._db() as conn:
            row = conn.execute(
                """
                SELECT * FROM people
                 WHERE name = ? COLLATE NOCASE
                    OR nickname = ? COLLATE NOCASE
                 ORDER BY id LIMIT 1
                """,
                (name, name),
            ).fetchone()
            if row is None:
                return {"status": "error", "error": "pessoa não encontrada"}
            person = self._person_dict(row)
            conn.execute("DELETE FROM people WHERE id=?", (row["id"],))
        photo = person.get("photo_path")
        if photo:
            try:
                Path(photo).unlink(missing_ok=True)
            except OSError:
                pass
        return {"status": "ok", "forgotten": person}
