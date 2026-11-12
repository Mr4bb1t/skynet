import os
import socket
import time
import numpy as np
from vision.camera_capture import CameraCapture
import config

def send_ptz(x, y):
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl"
               xmlns:tt="http://www.onvif.org/ver10/schema">
   <soap:Body>
      <ptz:ContinuousMove>
         <ptz:ProfileToken>IPCProfilesToken0</ptz:ProfileToken>
         <ptz:Velocity><tt:PanTilt x="{x:.2f}" y="{y:.2f}"/></ptz:Velocity>
      </ptz:ContinuousMove>
   </soap:Body>
</soap:Envelope>"""
    data = envelope.encode("utf-8")
    req = (
        f"POST /onvif/ptz_service HTTP/1.1\r\n"
        f"Host: 192.168.10.14:5000\r\n"
        f"Content-Type: application/soap+xml; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii") + data

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(("192.168.10.14", 5000))
        s.sendall(req)
        s.recv(1024)
    finally:
        s.close()

def move_duration(x, y, duration_sec):
    send_ptz(x, y)
    time.sleep(duration_sec)
    send_ptz(0.0, 0.0)

def test_physical():
    cam = CameraCapture(
        url="rtsp://admin:E33426244b@192.168.10.14:554/onvif1",
        transport="udp",
        max_width=640,
        fps=5.0,
        skip_probe=True,
    )
    cam.start()
    print("Aguardando conexao de video...")
    for _ in range(25):
        f = cam.get_latest(max_age=2.0)
        if f is not None:
            break
        time.sleep(0.2)

    f1 = cam.get_latest(max_age=2.0)
    if f1 is None:
        print("Erro: camera nao entregou frame")
        cam.stop()
        return

    print("Capturado frame 1!")
    print("Movendo PAN para a DIREITA por 0.5s...")
    move_duration(1.0, 0.0, 0.5)
    time.sleep(1.0)

    f2 = cam.get_latest(max_age=2.0)
    diff = np.mean(np.abs(f1.astype(float) - f2.astype(float)))
    print(f"Diferenca media entre os frames: {diff:.2f}")

    if diff > 5.0:
        print(">>> SUCESSO! A CAMERA SE MOVEU FISICAMENTE! <<<")
    else:
        print(">>> A camera NAO se moveu. <<<")

    print("Retornando PAN para a ESQUERDA por 0.5s...")
    move_duration(-1.0, 0.0, 0.5)
    time.sleep(1.0)
    cam.stop()

if __name__ == "__main__":
    test_physical()
