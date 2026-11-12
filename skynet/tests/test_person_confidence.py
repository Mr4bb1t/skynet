#!/usr/bin/env python3
"""Testes do nível de confiança do reconhecimento facial."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vision.person_memory import DetectedFace, distance_confidence  # noqa: E402


class FaceConfidenceTest(unittest.TestCase):
    def test_mapping_is_monotonic_and_centered_on_threshold(self) -> None:
        threshold = 0.6
        self.assertGreater(distance_confidence(0.0, threshold), 0.98)
        self.assertAlmostEqual(distance_confidence(0.6, threshold), 0.5, places=6)
        self.assertLess(distance_confidence(0.9, threshold), 0.1)

        previous = 1.1
        for distance in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            value = distance_confidence(distance, threshold)
            self.assertIsNotNone(value)
            self.assertLess(value, previous)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
            previous = value

    def test_none_distance(self) -> None:
        self.assertIsNone(distance_confidence(None, 0.6))

    def test_detected_face_defaults_unknown(self) -> None:
        face = DetectedFace(face_index=0, bbox=[0, 0, 10, 10], location=(0, 10, 10, 0))
        self.assertFalse(face.known)
        self.assertEqual(face.confidence, 0.0)
        self.assertIsNone(face.match_confidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
