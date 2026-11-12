import time
from vision.camera_capture import CameraCapture
from vision.image_processor import ImageProcessor

def test():
    url = "rtsp://admin:E33426244b@192.168.10.14:554/onvif1"
    print("Iniciando CameraCapture rapida (skip_probe=True)...")
    cam = CameraCapture(
        url,
        transport="udp",
        max_width=640,
        fps=5.0,
        skip_probe=True,
        fallback_width=640,
        fallback_height=360,
    )
    cam.start()
    proc = ImageProcessor(max_width=640, max_height=360, jpeg_quality=70)
    try:
        for i in range(80):
            frame = cam.get_latest(max_age=5.0)
            if frame is not None:
                pf = proc.process(frame)
                print(f"Sucesso no tick {i}! Frame: {frame.width}x{frame.height}, JPEG: {len(pf.jpeg)} bytes")
                return True
            time.sleep(0.1)
        print("Timeout.")
        return False
    finally:
        cam.stop()

if __name__ == "__main__":
    test()

