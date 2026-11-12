#!/usr/bin/env python3
"""Testes de parsing RTSP/SDP/RTP do microfone da câmera (sem rede)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from audio.sources import CameraAudioSource  # noqa: E402

SDP = """v=0
o=- 0 0 IN IP4 192.168.10.14
s=RTSP Session
m=video 0 RTP/AVP 96
a=control:track1
a=rtpmap:96 H264/90000
m=audio 0 RTP/AVP 8
a=control:track2
a=rtpmap:8 PCMA/8000
"""


class CameraRtspParsingTest(unittest.TestCase):
    def test_parse_sdp_audio(self) -> None:
        track, codec, rate = CameraAudioSource._parse_sdp_audio(
            SDP,
            "rtsp://192.168.10.14:554/onvif1",
            "192.168.10.14",
            554,
        )
        self.assertEqual(track, "rtsp://192.168.10.14:554/onvif1/track2")
        self.assertEqual(codec, "PCMA")
        self.assertEqual(rate, 8000)

    def test_parse_sdp_absolute_control(self) -> None:
        sdp = SDP.replace(
            "a=control:track2", "a=control:rtsp://192.168.10.14:554/audio"
        )
        track, codec, rate = CameraAudioSource._parse_sdp_audio(
            sdp, "rtsp://192.168.10.14:554/onvif1", "192.168.10.14", 554
        )
        self.assertEqual(track, "rtsp://192.168.10.14:554/audio")
        self.assertEqual(codec, "PCMA")

    def test_parse_sdp_without_audio_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            CameraAudioSource._parse_sdp_audio(
                "m=video 0 RTP/AVP 96\na=control:track1\n",
                "rtsp://192.168.10.14:554/onvif1",
                "192.168.10.14",
                554,
            )

    def test_rtp_payload_basic(self) -> None:
        packet = bytes([0x80, 8, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]) + b"\xd5\x00\x01"
        self.assertEqual(CameraAudioSource._rtp_payload(packet), b"\xd5\x00\x01")

    def test_rtp_payload_with_csrc_extension_padding(self) -> None:
        # CC=1 (4 bytes), extensão de 1 palavra (4 bytes), padding de 2 bytes.
        header = bytes([0xB1, 8, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1])
        csrc = b"\x00\x00\x00\x02"
        extension = b"\xbe\xde\x00\x01" + b"\x00\x00\x00\x00"
        payload = b"\xaa\xbb"
        packet = header + csrc + extension + payload + b"\x00\x02"
        self.assertEqual(
            CameraAudioSource._rtp_payload(packet), payload
        )

    def test_decode_resamples_8k_to_16k(self) -> None:
        source = CameraAudioSource(gain=1.0)
        source._codec = "PCMA"
        source._codec_rate = 8000
        # 160 amostras A-law a 8 kHz -> 16 kHz = ~320 amostras = ~640 bytes.
        pcm = source._decode(b"\xd5" * 160)
        self.assertIsNotNone(pcm)
        self.assertGreater(len(pcm), 600)
        self.assertLessEqual(len(pcm), 680)


if __name__ == "__main__":
    unittest.main(verbosity=2)
