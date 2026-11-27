#include "../pcm_convert.h"

#include <cassert>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <vector>

namespace {

void TestClampFloatToS16() {
    assert(ClampFloatToS16(0.0f) == 0);
    assert(ClampFloatToS16(1.0f) == 32767);
    assert(ClampFloatToS16(2.5f) == 32767);
    assert(ClampFloatToS16(-1.0f) == -32768);
    assert(ClampFloatToS16(-3.0f) == -32768);
    assert(ClampFloatToS16(0.5f) == 16384);
    assert(ClampFloatToS16(-0.5f) == -16384);
    assert(ClampFloatToS16(std::numeric_limits<float>::quiet_NaN()) == 0);
    assert(ClampFloatToS16(std::numeric_limits<float>::infinity()) == 32767);
    assert(ClampFloatToS16(-std::numeric_limits<float>::infinity()) == -32768);
}

void TestConvertFloat32MonoStereoAndMultichannel() {
    std::vector<int16_t> out;

    // Mono float32 -> duplicated L/R
    const float mono[2] = {0.5f, -0.5f};
    uint64_t nz = ConvertToStereoS16(
        reinterpret_cast<const uint8_t*>(mono),
        2,
        PcmInputFormat{1, 32, PcmFormatTag::kFloat32},
        out
    );
    assert(out.size() == 4);
    assert(out[0] == 16384 && out[1] == 16384);
    assert(out[2] == -16384 && out[3] == -16384);
    assert(nz == 4);

    // 5.1 float32 -> L + 0.7071*C, R + 0.7071*C with NaN safety on ch0
    const float surround[6] = {
        std::numeric_limits<float>::quiet_NaN(), 0.25f, 0.5f, 0.0f, 0.9f, 0.9f
    };
    nz = ConvertToStereoS16(
        reinterpret_cast<const uint8_t*>(surround),
        1,
        PcmInputFormat{6, 32, PcmFormatTag::kFloat32},
        out
    );
    assert(out.size() == 2);
    // NaN + C stays NaN -> guarded to 0 on L; R = 0.25 + 0.5 * 0.70710678 = 0.60355339 -> 19777
    assert(out[0] == 0);
    assert(out[1] == 19777);
    assert(nz == 1);
}

void TestConvertPcm16And24And32() {
    std::vector<int16_t> out;

    // Stereo PCM16 passthrough
    const int16_t s16[4] = {1000, -2000, 0, 32767};
    uint64_t nz = ConvertToStereoS16(
        reinterpret_cast<const uint8_t*>(s16),
        2,
        PcmInputFormat{2, 16, PcmFormatTag::kPcmInt},
        out
    );
    assert(out.size() == 4);
    assert(out[0] == 1000 && out[1] == -2000 && out[2] == 0 && out[3] == 32767);
    assert(nz == 3);

    // Stereo 24-bit little-endian: +0.5 full scale = 0x400000 -> bytes {0x00, 0x00, 0x40},
    // -0.5 full scale = 0xC00000 -> bytes {0x00, 0x00, 0xC0}
    const uint8_t s24[6] = {0x00, 0x00, 0x40, 0x00, 0x00, 0xC0};
    nz = ConvertToStereoS16(
        s24,
        1,
        PcmInputFormat{2, 24, PcmFormatTag::kPcmInt},
        out
    );
    assert(out.size() == 2);
    assert(out[0] == 16384 && out[1] == -16384);
    assert(nz == 2);

    // Mono 32-bit signed PCM: INT32_MIN -> -32768
    const int32_t s32[1] = {std::numeric_limits<int32_t>::min()};
    nz = ConvertToStereoS16(
        reinterpret_cast<const uint8_t*>(s32),
        1,
        PcmInputFormat{1, 32, PcmFormatTag::kPcmInt},
        out
    );
    assert(out.size() == 2);
    assert(out[0] == -32768 && out[1] == -32768);
    assert(nz == 2);
}

void TestParseCaptureArgs() {
    CaptureCliArgs args;
    std::string err;

    const char* ok_argv[] = {
        "sessiontape-capture-win.exe", "--pid", "4242", "--sample-rate", "44100", "--debug", "--no-metrics"
    };
    assert(ParseCaptureArgs(7, ok_argv, args, err));
    assert(args.pid == 4242);
    assert(args.sample_rate == 44100);
    assert(args.debug);
    assert(!args.metrics);

    const char* missing_pid[] = {"sessiontape-capture-win.exe", "--sample-rate", "48000"};
    assert(!ParseCaptureArgs(3, missing_pid, args, err));

    const char* bad_pid_alpha[] = {"sessiontape-capture-win.exe", "--pid", "abc"};
    assert(!ParseCaptureArgs(3, bad_pid_alpha, args, err));

    const char* bad_pid_neg[] = {"sessiontape-capture-win.exe", "--pid", "-5"};
    assert(!ParseCaptureArgs(3, bad_pid_neg, args, err));

    const char* bad_pid_zero[] = {"sessiontape-capture-win.exe", "--pid", "0"};
    assert(!ParseCaptureArgs(3, bad_pid_zero, args, err));

    const char* bad_unknown[] = {"sessiontape-capture-win.exe", "--pid", "100", "--bogus"};
    assert(!ParseCaptureArgs(4, bad_unknown, args, err));
}

void TestFormatCaptureMetricsLine() {
    const std::string line = FormatCaptureMetricsLine(2, 95, 384000, 1);
    assert(line == "CAPTURE drops=2 cb=95 bytes=384000 trunc=1");
}

} // namespace

int main() {
    TestClampFloatToS16();
    TestConvertFloat32MonoStereoAndMultichannel();
    TestConvertPcm16And24And32();
    TestParseCaptureArgs();
    TestFormatCaptureMetricsLine();
    std::cout << "All windows-capture unit tests passed.\n";
    return 0;
}
