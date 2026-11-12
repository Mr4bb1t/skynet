#!/usr/bin/env bash
# =============================================================================
# Compila o WebRTC AEC3 (webrtc-audio-processing 1.3) + a fachada libskynet_aec.so
#
# Nao existe wheel AEC3 para armhf; por isso compilamos uma vez no proprio box.
# E idempotente: se o AEC3 ja estiver instalado, so recompila a fachada.
#
# Uso (como root, no box):
#   bash /root/skynet/audio/native/build_aec.sh
# =============================================================================

set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
WRTC_VERSION="${WRTC_VERSION:-1.3}"
BUILD_ROOT="${AEC_BUILD_ROOT:-/root/aec-build}"
JOBS="${AEC_JOBS:-$(nproc)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRTC_TARBALL="webrtc-audio-processing-${WRTC_VERSION}.tar.gz"
WRTC_URL="https://freedesktop.org/software/pulseaudio/webrtc-audio-processing/${WRTC_TARBALL}"
WRTC_SHA256="95552fc17faa0202133707bbb3727e8c2cf64d4266fe31bfdb2298d769c1db75"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERRO: rode como root (precisa instalar em $PREFIX e rodar ldconfig)." >&2
    exit 1
fi

have_webrtc() {
    PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}" \
        pkg-config --exists webrtc-audio-processing-1 2>/dev/null
}

if ! have_webrtc; then
    echo "=== [1/3] Dependencias e WebRTC AEC3 $WRTC_VERSION ==="
    apt-get install -y meson ninja-build pkg-config git

    mkdir -p "$BUILD_ROOT"
    cd "$BUILD_ROOT"
    if [ ! -f "$WRTC_TARBALL" ]; then
        echo "Baixando $WRTC_URL"
        curl -L --fail --max-time 180 -o "$WRTC_TARBALL" "$WRTC_URL"
    fi
    echo "$WRTC_SHA256  $WRTC_TARBALL" | sha256sum -c -
    if [ ! -d "webrtc-audio-processing-${WRTC_VERSION}" ]; then
        tar xzf "$WRTC_TARBALL"
    fi

    cd "webrtc-audio-processing-${WRTC_VERSION}"
    if [ ! -d build ]; then
        meson setup build --buildtype=release --prefix="$PREFIX" -Ddefault_library=shared
    fi
    echo "Compilando (pode levar 20-40 min em armhf)..."
    ninja -C build -j"$JOBS"
    ninja -C build install
    ldconfig
else
    echo "=== [1/3] WebRTC AEC3 ja instalado; pulando build da biblioteca ==="
fi

echo "=== [2/3] Compilando libskynet_aec.so ==="
if have_webrtc; then
    WRTC_CFLAGS="$(PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}" pkg-config --cflags webrtc-audio-processing-1)"
    WRTC_LIBS="$(PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}" pkg-config --libs webrtc-audio-processing-1)"
else
    WRTC_CFLAGS="-I$PREFIX/include/webrtc-audio-processing-1"
    WRTC_LIBS="-L$PREFIX/lib -lwebrtc-audio-processing-1"
fi

# shellcheck disable=SC2086
g++ -O2 -fPIC -shared -std=c++17 \
    -o "$PREFIX/lib/libskynet_aec.so" \
    "$SCRIPT_DIR/skynet_aec.cpp" \
    $WRTC_CFLAGS $WRTC_LIBS \
    -Wl,-rpath,"$PREFIX/lib"

ldconfig

echo "=== [3/3] Verificando ==="
python3 - <<PY
import ctypes
lib = ctypes.CDLL("$PREFIX/lib/libskynet_aec.so")
lib.skynet_aec_create.restype = ctypes.c_void_p
lib.skynet_aec_create.argtypes = [ctypes.c_int, ctypes.c_int]
handle = lib.skynet_aec_create(16000, 0)
assert handle, "skynet_aec_create falhou"
lib.skynet_aec_destroy.argtypes = [ctypes.c_void_p]
lib.skynet_aec_destroy(handle)
print("AEC3 OK: libskynet_aec.so carregada e inicializada a 16 kHz")
PY

echo
echo "Pronto. Instalado em $PREFIX/lib/libskynet_aec.so"
echo "Para testar: python3 /root/skynet/tests/test_aec_offline.py"
