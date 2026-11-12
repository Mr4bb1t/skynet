import time
from vision.config import load_settings
from vision.camera_capture import CameraCapture
from vision.image_processor import ImageProcessor

def test():
    settings = load_settings()
    url = "rtsp://admin:E33426244b@192.168.10.14:554/onvif1"
    print("URL da camera:", url)
    cam = CameraCapture(url, transport="udp", max_width=640)
    cam.start()
    try:
        print("Aguardando frame da camera...")
        for i in range(120):
            frame = cam.get_latest(max_age=5.0)
            if frame is not None:
                print(f"Frame recebido com sucesso no tick {i}! Tam: {frame.width}x{frame.height}, seq={frame.seq}")
                proc = ImageProcessor(settings)
                p = proc.process(frame)
                print(f"Frame processado em JPEG: {len(p.jpeg_bytes)} bytes")
                return True
            time.sleep(0.1)
        print("Timeout aguardando frame.")
        return False
    finally:
        cam.stop()

if __name__ == "__main__":
    test()
