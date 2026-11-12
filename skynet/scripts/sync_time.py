#!/usr/bin/env python3
"""Sincroniza o relogio do TV Box antes de conexoes TLS.

O box nao tem RTC confiavel: a cada boot o relogio pode voltar para a data da
imagem (Jun/2026), e o certificado do Gemini aparece como "not yet valid".
Este script consulta NTP (UDP 123) e, se falhar, usa o cabecalho HTTP Date
(porta 80). Nao depende de TLS, justamente porque o relogio pode estar errado.

Tambem ajusta o fuso horario (padrao America/Sao_Paulo, Londrina/PR) e grava a
hora no RTC quando ele existe.

Fica em /root/sync_time.py e e chamado pelo wrapper /usr/local/bin/skynet e por
main.py antes de subir o Skynet. O marcador /var/run/time_synced evita consultas
repetidas dentro da janela --max-age (mas e ignorado se o relogio voltou no
tempo, como acontece num reboot sem RTC).
"""

from __future__ import annotations

import argparse
import email.utils
import http.client
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MARKER = Path("/var/run/time_synced")
DEFAULT_TIMEZONE = "America/Sao_Paulo"
# Qualquer "hora da internet" anterior a esta data e considerada invalida.
# E tambem a data minima aceitavel para o relogio local.
MIN_VALID_EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()

# Apenas IPs: DNS pode estar quebrado no boot e nao deve segurar a inicializacao.
NTP_HOSTS = (
    "162.159.200.1",
    "162.159.200.123",
    "216.239.35.0",
    "216.239.35.4",
    "216.239.35.8",
)
# Cloudflare responde na porta 80 com o cabecalho Date (sem redirect para TLS).
HTTP_HOSTS = ("1.1.1.1", "1.0.0.1")


def ntp_time(host: str, timeout: float = 2.0) -> float | None:
    """Retorna o epoch UTC a partir da resposta NTP do host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"\x1b" + 47 * b"\x00", (host, 123))
        data, _ = sock.recvfrom(48)
        if len(data) < 48:
            return None
        seconds, fraction = struct.unpack("!II", data[40:48])
        if seconds == 0:
            return None
        return float(seconds - 2208988800) + fraction / 2**32
    finally:
        sock.close()


def http_time(host: str, timeout: float = 2.5) -> float | None:
    """Retorna o epoch UTC a partir do cabecalho HTTP Date (porta 80).

    Usa http.client direto para nao seguir redirect (o redirect do 1.1.1.1 e
    para HTTPS, que falharia justamente quando o relogio esta errado).
    """
    conn = http.client.HTTPConnection(host, 80, timeout=timeout)
    try:
        conn.request("HEAD", "/")
        response = conn.getresponse()
        header = response.getheader("Date")
        response.read()
    finally:
        conn.close()
    if not header:
        return None
    parsed = email.utils.parsedate_to_datetime(header)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def query_time(deadline: float, timeout: float, http_timeout: float, quiet: bool) -> float | None:
    """Tenta NTP (IPs primeiro) e cai para HTTP Date se necessario.

    `deadline` e um instante de time.monotonic(); nenhuma consulta ultrapassa
    esse orcamento, para o boot nao ficar preso quando a rede esta ruim.
    """

    def remaining() -> float:
        return deadline - time.monotonic()

    for host in NTP_HOSTS:
        left = remaining()
        if left <= 0:
            return None
        try:
            epoch = ntp_time(host, min(timeout, left))
        except Exception as exc:
            if not quiet:
                print(f"NTP {host} falhou: {exc}")
            continue
        if epoch and epoch >= MIN_VALID_EPOCH:
            if not quiet:
                print(f"NTP {host}: {datetime.fromtimestamp(epoch, timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
            return epoch

    for host in HTTP_HOSTS:
        left = remaining()
        if left <= 0:
            return None
        try:
            epoch = http_time(host, min(http_timeout, left))
        except Exception as exc:
            if not quiet:
                print(f"HTTP {host} falhou: {exc}")
            continue
        if epoch and epoch >= MIN_VALID_EPOCH:
            if not quiet:
                print(f"HTTP {host}: {datetime.fromtimestamp(epoch, timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
            return epoch

    return None


def apply_epoch(epoch: float) -> bool:
    """Ajusta o relogio do sistema (UTC) e, se houver, o RTC."""
    target = datetime.fromtimestamp(epoch, timezone.utc)
    result = subprocess.run(
        ["date", "-u", "-s", target.strftime("%Y-%m-%d %H:%M:%S")],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(f"Falha ao ajustar o relogio: {detail or 'erro desconhecido'}")
        return False

    # RTC pode nao existir/ser gravavel; nao e fatal.
    hwclock = shutil.which("hwclock")
    if hwclock:
        subprocess.run([hwclock, "--systohc"], capture_output=True, check=False)
    return True


def set_timezone(tz_name: str, quiet: bool = False) -> bool:
    """Aponta /etc/localtime para o fuso pedido (Londrina -> America/Sao_Paulo)."""
    zone = Path("/usr/share/zoneinfo") / tz_name
    localtime = Path("/etc/localtime")
    if not zone.is_file():
        if not quiet:
            print(f"Fuso {tz_name} indisponivel em /usr/share/zoneinfo; mantendo o atual.")
        return False

    try:
        if localtime.resolve() == zone.resolve():
            os.environ["TZ"] = tz_name
            if hasattr(time, "tzset"):
                time.tzset()
            return True
    except OSError:
        pass

    try:
        temp = localtime.parent / ".localtime.new"
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        temp.symlink_to(zone)
        os.replace(temp, localtime)
        Path("/etc/timezone").write_text(tz_name + "\n", encoding="utf-8")
    except OSError as exc:
        if not quiet:
            print(f"Nao foi possivel ajustar o fuso para {tz_name}: {exc}")
        return False

    os.environ["TZ"] = tz_name
    if hasattr(time, "tzset"):
        time.tzset()
    if not quiet:
        print(f"Fuso horario ajustado para {tz_name}")
    return True


def last_sync(marker: Path) -> float | None:
    """Le o epoch da ultima sincronizacao gravado no marcador."""
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    try:
        return float(raw)
    except ValueError:
        pass

    # Compatibilidade com o formato antigo (ISO 8601).
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sincroniza o relogio do box pela internet.")
    parser.add_argument(
        "--timezone",
        default=os.getenv("SKYNET_TIMEZONE", DEFAULT_TIMEZONE),
        help=f"Fuso IANA a aplicar (padrao: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=300.0,
        help="Segundos em que uma sincronizacao recente e reaproveitada (padrao: 300)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Tentativas completas dentro do --deadline (padrao: 3)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=10.0,
        help="Orcamento total de tempo para sincronizar, em segundos (padrao: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.2,
        help="Timeout por host NTP, limitado pelo --deadline (padrao: 1.2s)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=2.5,
        help="Timeout por URL de fallback HTTP, limitado pelo --deadline (padrao: 2.5s)",
    )
    parser.add_argument("--force", action="store_true", help="Sincroniza mesmo dentro da janela --max-age")
    parser.add_argument("--quiet", action="store_true", help="So imprime erros")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    now = time.time()
    recorded = last_sync(MARKER)
    # Se o relogio voltou no tempo (reboot sem RTC), age fica negativo e a
    # sincronizacao antiga nao vale mais, mesmo com o marcador em disco.
    if not args.force and recorded is not None:
        age = now - recorded
        if now >= MIN_VALID_EPOCH and 0 <= age <= args.max_age:
            if not args.quiet:
                print(f"Relogio ja sincronizado ha {int(age)}s; mantendo.")
            set_timezone(args.timezone, quiet=args.quiet)
            return 0

    epoch: float | None = None
    deadline = time.monotonic() + max(0.0, args.deadline)
    for attempt in range(max(1, args.retries)):
        epoch = query_time(deadline, args.timeout, args.http_timeout, args.quiet)
        if epoch is not None:
            break
        left = deadline - time.monotonic()
        if attempt + 1 >= args.retries or left <= 0:
            break
        time.sleep(min(1.5 * (attempt + 1), left))

    if epoch is None:
        print("Nao foi possivel sincronizar o relogio pela internet")
        return 1

    if not apply_epoch(epoch):
        return 1

    set_timezone(args.timezone, quiet=args.quiet)

    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(f"{epoch:.3f}\n", encoding="utf-8")
    except OSError as exc:
        if not args.quiet:
            print(f"Aviso: nao foi possivel gravar {MARKER}: {exc}")

    if not args.quiet:
        local = datetime.now().astimezone()
        print(f"Relogio ajustado para {local:%Y-%m-%d %H:%M:%S %Z}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
