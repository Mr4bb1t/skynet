// =============================================================================
// skynet_aec.cpp - fachada C minimalista sobre o WebRTC AEC3 (APM 1.3).
//
// O Python nao consegue chamar C++ com facilidade; expomos apenas 4 funcoes
// com ABI C e buffers int16 mono de 10 ms (160 amostras a 16 kHz).
//
// Build: veja build_aec.sh (linka contra libwebrtc-audio-processing-1).
// =============================================================================

#include <cstdint>
#include <vector>

// Exigido pelos headers do WebRTC em builds standalone.
#define WEBRTC_POSIX 1
#include <modules/audio_processing/include/audio_processing.h>

namespace {

struct SkynetAec {
    webrtc::AudioProcessing* apm = nullptr;
    webrtc::StreamConfig config;
    int frame_samples = 0;
    std::vector<int16_t> reverse_out;

    explicit SkynetAec(int sample_rate)
        : config(sample_rate, 1), frame_samples(sample_rate / 100) {
        reverse_out.resize(static_cast<size_t>(frame_samples));
    }

    ~SkynetAec() {
        delete apm;
        apm = nullptr;
    }
};

}  // namespace

extern "C" {

void* skynet_aec_create(int sample_rate, int delay_ms) {
    // O pipeline do Skynet usa 16 kHz no microfone e no envio ao Gemini.
    if (sample_rate != 16000) {
        return nullptr;
    }

    webrtc::AudioProcessing* apm = webrtc::AudioProcessingBuilder().Create();
    if (apm == nullptr) {
        return nullptr;
    }

    webrtc::AudioProcessing::Config cfg;
    cfg.echo_canceller.enabled = true;
    cfg.echo_canceller.mobile_mode = false;  // false = AEC3 (true = AECM)
    apm->ApplyConfig(cfg);
    if (delay_ms > 0) {
        apm->set_stream_delay_ms(delay_ms);
    }

    SkynetAec* state = new SkynetAec(sample_rate);
    state->apm = apm;
    return state;
}

void skynet_aec_destroy(void* handle) {
    delete static_cast<SkynetAec*>(handle);
}

// Alimenta a referencia (render/far-end) de um frame de 10 ms. Deve ser
// chamada assim que o audio e entregue ao alto-falante, antes do eco chegar
// ao microfone. O AEC3 guarda o historico e estima o atraso acustico.
int skynet_aec_far(void* handle, const int16_t* far, int frames) {
    SkynetAec* state = static_cast<SkynetAec*>(handle);
    if (state == nullptr || state->apm == nullptr || far == nullptr) {
        return -1;
    }
    if (frames != state->frame_samples) {
        return -2;
    }
    return state->apm->ProcessReverseStream(
        far, state->config, state->config, state->reverse_out.data());
}

// Processa o microfone (capture/near-end) de um frame de 10 ms.
int skynet_aec_near(void* handle, const int16_t* near, int16_t* out, int frames) {
    SkynetAec* state = static_cast<SkynetAec*>(handle);
    if (state == nullptr || state->apm == nullptr || near == nullptr || out == nullptr) {
        return -1;
    }
    if (frames != state->frame_samples) {
        return -2;
    }
    return state->apm->ProcessStream(near, state->config, state->config, out);
}

// Compatibilidade: processa far+near no mesmo frame (usado em testes offline
// e por integracoes antigas). Em tempo real prefira far()/near() separados.
int skynet_aec_process(void* handle, const int16_t* near, const int16_t* far,
                       int16_t* out, int frames) {
    int rc = skynet_aec_far(handle, far, frames);
    if (rc != 0) {
        return rc;
    }
    return skynet_aec_near(handle, near, out, frames);
}

int skynet_aec_set_delay(void* handle, int delay_ms) {
    SkynetAec* state = static_cast<SkynetAec*>(handle);
    if (state == nullptr || state->apm == nullptr) {
        return -1;
    }
    return state->apm->set_stream_delay_ms(delay_ms);
}

}  // extern "C"
