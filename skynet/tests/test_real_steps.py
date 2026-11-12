import sys
import os
import socket
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import config

IP = config.CAMERA_IP
PORT = config.CAMERA_ONVIF_PORT
TOKEN = config.ONVIF_PROFILE_TOKEN

def send_soap(body_xml):
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl"
               xmlns:tt="http://www.onvif.org/ver10/schema">
   <soap:Body>
      {body_xml}
   </soap:Body>
</soap:Envelope>"""
    data = envelope.encode("utf-8")
    req = (
        f"POST /onvif/ptz_service HTTP/1.1\r\n"
        f"Host: {IP}:{PORT}\r\n"
        f"Content-Type: application/soap+xml; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii") + data

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect((IP, PORT))
        s.sendall(req)
        resp = s.recv(2048)
        return resp
    finally:
        s.close()

def continuous_step(x, y, duration=0.25):
    """Executa um passo mecânico nítido e real: move e para."""
    move_body = f"""<ptz:ContinuousMove>
      <ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>
      <ptz:Velocity><tt:PanTilt x="{x:.2f}" y="{y:.2f}"/></ptz:Velocity>
   </ptz:ContinuousMove>"""
    send_soap(move_body)
    time.sleep(duration)
    stop_body = f"""<ptz:ContinuousMove>
      <ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>
      <ptz:Velocity><tt:PanTilt x="0.0" y="0.0"/></ptz:Velocity>
   </ptz:ContinuousMove>"""
    send_soap(stop_body)

def main():
    print("Testando 3 passos para a DIREITA (0.25s cada)...")
    for i in range(3):
        print(f"Passo direita {i+1}...")
        continuous_step(1.0, 0.0, duration=0.25)
        time.sleep(0.3)

    time.sleep(1.0)

    print("Testando 3 passos para a ESQUERDA (0.25s cada)...")
    for i in range(3):
        print(f"Passo esquerda {i+1}...")
        continuous_step(-1.0, 0.0, duration=0.25)
        time.sleep(0.3)

    time.sleep(1.0)

    print("Testando 2 passos para CIMA...")
    for i in range(2):
        print(f"Passo cima {i+1}...")
        continuous_step(0.0, 1.0, duration=0.20)
        time.sleep(0.3)

    time.sleep(1.0)

    print("Testando 2 passos para BAIXO...")
    for i in range(2):
        print(f"Passo baixo {i+1}...")
        continuous_step(0.0, -1.0, duration=0.20)
        time.sleep(0.3)

    print("\nTeste de passos concluido!")

if __name__ == "__main__":
    main()
