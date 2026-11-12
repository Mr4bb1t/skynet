import socket
import time
import config

TOKEN = config.ONVIF_PROFILE_TOKEN
IP = config.CAMERA_IP
PORT = config.CAMERA_ONVIF_PORT

def send_soap(body_xml, action="ContinuousMove"):
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
        resp = s.recv(2048).decode("utf-8", "ignore")
        return resp
    finally:
        s.close()

def test_moves():
    print("=== TESTE A: ContinuousMove 1.0 por 0.5s ===")
    body_move = f"""<ptz:ContinuousMove>
      <ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>
      <ptz:Velocity><tt:PanTilt x="1.0" y="0.0"/></ptz:Velocity>
   </ptz:ContinuousMove>"""
    resp = send_soap(body_move)
    print("A response:", resp[:200])
    time.sleep(0.5)
    body_stop = f"""<ptz:ContinuousMove>
      <ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>
      <ptz:Velocity><tt:PanTilt x="0.0" y="0.0"/></ptz:Velocity>
   </ptz:ContinuousMove>"""
    resp_stop = send_soap(body_stop)
    print("A stop response:", resp_stop[:200])

    time.sleep(1.0)

    print("\n=== TESTE B: ContinuousMove com Timeout PT0.3S ===")
    body_timeout = f"""<ptz:ContinuousMove>
      <ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>
      <ptz:Velocity><tt:PanTilt x="-1.0" y="0.0"/></ptz:Velocity>
      <ptz:Timeout>PT0.3S</ptz:Timeout>
   </ptz:ContinuousMove>"""
    resp_b = send_soap(body_timeout)
    print("B response:", resp_b[:200])

    time.sleep(1.0)

    print("\n=== TESTE C: RelativeMove (Passo discreto) ===")
    body_rel = f"""<ptz:RelativeMove>
      <ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>
      <ptz:Translation><tt:PanTilt x="0.1" y="0.0"/></ptz:Translation>
   </ptz:RelativeMove>"""
    resp_c = send_soap(body_rel, action="RelativeMove")
    print("C response:", resp_c[:200])

if __name__ == "__main__":
    test_moves()

