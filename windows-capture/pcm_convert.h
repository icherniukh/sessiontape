#pragma once

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

constexpr int kExitGeneralError         = 1;
constexpr int kExitConfigError          = 2;
constexpr int kExitFormatMismatch       = 3;
constexpr int kExitDeviceOrStateChanged = 4;

enum class PcmFormatTag : uint8_t {
    kUnknown = 0,
    kFloat32 = 1,
    kPcmInt  = 2,
};

struct PcmInputFormat {
    uint16_t channels        = 0;
    uint16_t bits_per_sample = 0;
    PcmFormatTag tag         = PcmFormatTag::kUnknown;
};

struct CaptureCliArgs {
    uint32_t pid         = 0;
    uint32_t sample_rate = 48000;
    bool debug           = false;
    bool metrics         = true;
};

inline int16_t ClampFloatToS16(float sample) {
    // NaN fails self-equality under IEEE-754.
    if (!(sample == sample)) {
        return 0;
    }
    const float scaled = sample * 32768.0f;
    if (scaled >= 32767.0f) {
        return 32767;
    }
    if (scaled <= -32768.0f) {
        return -32768;
    }
    return static_cast<int16_t>(std::lrintf(scaled));
}

// Converts captured interleaved frames (IEEE float32 or PCM 16/24/32-bit, any channel count)
// to stereo s16le. When multichannel audio arrives without WASAPI downmix, mixes center (ch 2)
// into L and R at -3 dB. Returns the count of non-zero output samples written to `out`.
inline uint64_t ConvertToStereoS16(const uint8_t* data, uint32_t frames, const PcmInputFormat& fmt,
                                   std::vector<int16_t>& out) {
    const uint16_t nCh = fmt.channels;
    if (!data || nCh == 0 || frames == 0) {
        out.assign(static_cast<size_t>(frames) * 2, 0);
        return 0;
    }

    constexpr float kCenterGain = 0.70710678f;
    out.resize(static_cast<size_t>(frames) * 2);
    uint64_t nonZero = 0;

    auto writeStereo = [&](uint32_t idx, int16_t left, int16_t right) {
        out[static_cast<size_t>(idx) * 2]     = left;
        out[static_cast<size_t>(idx) * 2 + 1] = right;
        if (left != 0) ++nonZero;
        if (right != 0) ++nonZero;
    };

    if (fmt.tag == PcmFormatTag::kFloat32 && fmt.bits_per_sample == 32) {
        const auto* p = reinterpret_cast<const float*>(data);
        for (uint32_t i = 0; i < frames; ++i) {
            const float* frame = p + static_cast<size_t>(i) * nCh;
            float L = frame[0];
            float R = (nCh > 1) ? frame[1] : frame[0];
            if (nCh > 2) {
                const float C = frame[2] * kCenterGain;
                L += C;
                R += C;
            }
            writeStereo(i, ClampFloatToS16(L), ClampFloatToS16(R));
        }
    } else if (fmt.tag == PcmFormatTag::kPcmInt && fmt.bits_per_sample == 16) {
        const auto* p = reinterpret_cast<const int16_t*>(data);
        for (uint32_t i = 0; i < frames; ++i) {
            const int16_t* frame = p + static_cast<size_t>(i) * nCh;
            if (nCh <= 2) {
                writeStereo(i, frame[0], (nCh > 1) ? frame[1] : frame[0]);
            } else {
                const float C = (static_cast<float>(frame[2]) / 32768.0f) * kCenterGain;
                writeStereo(
                    i,
                    ClampFloatToS16(static_cast<float>(frame[0]) / 32768.0f + C),
                    ClampFloatToS16(static_cast<float>(frame[1]) / 32768.0f + C)
                );
            }
        }
    } else if (fmt.tag == PcmFormatTag::kPcmInt && fmt.bits_per_sample == 24) {
        const size_t frameBytes = static_cast<size_t>(nCh) * 3;
        auto read24Norm = [](const uint8_t* b) -> float {
            const int32_t v = static_cast<int32_t>(
                (static_cast<uint32_t>(b[0]) << 8) |
                (static_cast<uint32_t>(b[1]) << 16) |
                (static_cast<uint32_t>(b[2]) << 24));
            return static_cast<float>(v) / 2147483648.0f;
        };
        for (uint32_t i = 0; i < frames; ++i) {
            const uint8_t* frame = data + static_cast<size_t>(i) * frameBytes;
            float L = read24Norm(frame);
            float R = (nCh > 1) ? read24Norm(frame + 3) : L;
            if (nCh > 2) {
                const float C = read24Norm(frame + 6) * kCenterGain;
                L += C;
                R += C;
            }
            writeStereo(i, ClampFloatToS16(L), ClampFloatToS16(R));
        }
    } else if (fmt.tag == PcmFormatTag::kPcmInt && fmt.bits_per_sample == 32) {
        const auto* p = reinterpret_cast<const int32_t*>(data);
        for (uint32_t i = 0; i < frames; ++i) {
            const int32_t* frame = p + static_cast<size_t>(i) * nCh;
            float L = static_cast<float>(frame[0]) / 2147483648.0f;
            float R = (nCh > 1) ? static_cast<float>(frame[1]) / 2147483648.0f : L;
            if (nCh > 2) {
                const float C = (static_cast<float>(frame[2]) / 2147483648.0f) * kCenterGain;
                L += C;
                R += C;
            }
            writeStereo(i, ClampFloatToS16(L), ClampFloatToS16(R));
        }
    } else {
        out.assign(static_cast<size_t>(frames) * 2, 0);
        return 0;
    }

    return nonZero;
}

inline bool ParsePositiveU32(const char* text, uint32_t& out) {
    if (!text || *text == '\0' || *text == '-' || *text == '+') {
        return false;
    }
    char* end = nullptr;
    errno = 0;
    const unsigned long long parsed = std::strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        return false;
    }
    if (parsed == 0 || parsed > static_cast<unsigned long long>(std::numeric_limits<uint32_t>::max())) {
        return false;
    }
    out = static_cast<uint32_t>(parsed);
    return true;
}

inline bool ParseCaptureArgs(int argc, const char* const* argv, CaptureCliArgs& out,
                             std::string& error_msg) {
    out = CaptureCliArgs{};
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i] ? argv[i] : "";
        if (arg == "--pid") {
            if (i + 1 >= argc || !ParsePositiveU32(argv[++i], out.pid)) {
                error_msg = "Invalid or missing --pid value";
                return false;
            }
        } else if (arg == "--sample-rate") {
            if (i + 1 >= argc || !ParsePositiveU32(argv[++i], out.sample_rate)) {
                error_msg = "Invalid or missing --sample-rate value";
                return false;
            }
        } else if (arg == "--debug") {
            out.debug = true;
        } else if (arg == "--no-metrics") {
            out.metrics = false;
        } else {
            error_msg = "Unknown argument: " + arg;
            return false;
        }
    }

    if (out.pid == 0) {
        error_msg = "Missing required --pid <PID>";
        return false;
    }
    return true;
}

inline std::string FormatCaptureMetricsLine(uint64_t drops, uint64_t callbacks,
                                            uint64_t bytes_written, uint64_t discontinuities) {
    std::ostringstream oss;
    oss << "CAPTURE drops=" << drops
        << " cb=" << callbacks
        << " bytes=" << bytes_written
        << " trunc=" << discontinuities;
    return oss.str();
}
