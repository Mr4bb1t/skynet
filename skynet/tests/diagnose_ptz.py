import socket
import time
from camera.ptz import PTZController
import config

def test_full_move():
    p = PTZController()
    print("Enviando ContinuousMove pan=1.0 por 1 segundo...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((config.CAMERA_IP, config.CAMERA_ONVIF_PORT))
    s.sendall(p._build_packet(1.0, 0.0))
    resp = s.recv(2048).decode("utf-8", "ignore")
    print("RESPOSTA ContinuousMove:\n", resp)
    s.close()

    time.sleep(1.0)

    print("\nEnviando Stop...")
    # Monta Stop SOAP
    stop_envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl">
   <soap:Body>
      <ptz:Stop>
         <ptz:ProfileToken>{p.token}</ptz:ProfileToken>
         <ptz:PanTilt>true</ptz:PanTilt>
         <ptz:Zoom>false</ptz:Zoom>
      </ptz:Stop>
   </soap:Body>
</soap:Envelope>"""
    stop_pkt = (
        f"POST /onvif/ptz_service HTTP/1.1\r\n"
        f"Host: {p.host}:{p.port}\r\n"
        f"Content-Type: application/soap+xml; charset=utf-8\r\n"
        f"Content-Length: {len(stop_envelope.encode('utf-8'))}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii") + stop_envelope.encode("utf-8")

    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.connect((config.CAMERA_IP, config.CAMERA_ONVIF_PORT))
    s2.sendall(stop_pkt)
    resp_stop = s2.recv(2048).decode("utf-8", "ignore")
    print("RESPOSTA Stop:\n", resp_stop)
    s2.close()

if __name__ == "__main__":
    test_full_move()
