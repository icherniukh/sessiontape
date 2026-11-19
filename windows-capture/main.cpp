#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <initguid.h>
#include <functiondiscoverykeys_devpkey.h>
// audioclientactivationparams.h is only available in Windows SDK 10.0.20348+
// Define the required types manually for older SDKs.
#ifndef AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
typedef enum AUDIOCLIENT_ACTIVATION_TYPE {
    AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT             = 0,
    AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK    = 1,
} AUDIOCLIENT_ACTIVATION_TYPE;

typedef enum PROCESS_LOOPBACK_MODE {
    PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE   = 0,
    PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE   = 1,
} PROCESS_LOOPBACK_MODE;

typedef struct AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {
    DWORD                TargetProcessId;
    PROCESS_LOOPBACK_MODE ProcessLoopbackMode;
} AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS;

typedef struct AUDIOCLIENT_ACTIVATION_PARAMS {
    AUDIOCLIENT_ACTIVATION_TYPE ActivationType;
    union {
        AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS ProcessLoopbackParams;
    };
} AUDIOCLIENT_ACTIVATION_PARAMS;
#endif

#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

#ifndef AUDCLNT_E_DEVICE_IN_USE
#define AUDCLNT_E_DEVICE_IN_USE ((HRESULT)0x8889000AL)
#endif
#ifndef AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED
#define AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED ((HRESULT)0x8889000EL)
#endif
#ifndef AUDCLNT_E_UNSUPPORTED_FORMAT
#define AUDCLNT_E_UNSUPPORTED_FORMAT ((HRESULT)0x88890008L)
#endif
#ifndef AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
#define AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM 0x80000000
#endif
#ifndef AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
#define AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY 0x08000000
#endif

#include <wrl/implements.h>
#include <cstdint>
#include <iostream>
#include <io.h>
#include <fcntl.h>
#include <vector>
#include <string>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <atomic>

using namespace Microsoft::WRL;

static bool g_debug = false;

#define LOG_INFO(msg)  do { std::cerr << "INFO: "    << msg << "\n"; } while (0)
#define LOG_WARN(msg)  do { std::cerr << "WARNING: " << msg << "\n"; } while (0)
#define LOG_ERROR(msg) do { std::cerr << "ERROR: "   << msg << "\n"; } while (0)
#define LOG_DEBUG(msg) do { if (g_debug) { std::cerr << "DEBUG: " << msg << "\n"; } } while (0)

struct CoUninitGuard {
    ~CoUninitGuard() { CoUninitialize(); }
};

struct CoTaskMemGuard {
    WAVEFORMATEX* ptr = nullptr;
    ~CoTaskMemGuard() {
        if (ptr) CoTaskMemFree(ptr);
    }
    void reset(WAVEFORMATEX* next = nullptr) {
        if (ptr && ptr != next) CoTaskMemFree(ptr);
        ptr = next;
    }
};

// COM completion handler for ActivateAudioInterfaceAsync
class CActivateAudioInterfaceCompletionHandler :
    public RuntimeClass<RuntimeClassFlags<ClassicCom>, IActivateAudioInterfaceCompletionHandler, FtmBase>
{
public:
    CActivateAudioInterfaceCompletionHandler() {
        m_hActivateCompleted = CreateEvent(nullptr, FALSE, FALSE, nullptr);
        if (m_hActivateCompleted == nullptr) {
            LOG_ERROR("CreateEvent failed: " << GetLastError());
            m_initFailed = true;
        }
    }

    ~CActivateAudioInterfaceCompletionHandler() {
        if (m_hActivateCompleted) CloseHandle(m_hActivateCompleted);
    }

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* operation)
    {
        HRESULT hrActivate = S_OK;
        ComPtr<IUnknown> punkAudioClient;

        HRESULT hr = operation->GetActivateResult(&hrActivate, &punkAudioClient);
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate) && punkAudioClient) {
            punkAudioClient.As(&m_AudioClient);
        }

        if (m_hActivateCompleted) SetEvent(m_hActivateCompleted);
        return S_OK;
    }

    ComPtr<IAudioClient> m_AudioClient;
    HANDLE m_hActivateCompleted = nullptr;
    bool m_initFailed = false;
};

// Global state for IPC writer thread
std::queue<std::vector<short>> audioQueue;
std::mutex queueMutex;
std::condition_variable queueCV;
std::atomic<bool> isRunning{true};
const size_t MAX_QUEUE_CHUNKS = 5000; // ~100 seconds of audio to prevent OOM

static void StopWriter() {
    {
        std::lock_guard<std::mutex> lock(queueMutex);
        isRunning = false;
    }
    queueCV.notify_all();
}

void WriterThread() {
    while (true) {
        std::vector<short> chunk;
        {
            std::unique_lock<std::mutex> lock(queueMutex);
            queueCV.wait(lock, [] { return !audioQueue.empty() || !isRunning; });

            if (!isRunning && audioQueue.empty()) break;
            if (audioQueue.empty()) continue;

            chunk = std::move(audioQueue.front());
            audioQueue.pop();
        }

        // On Windows blocking pipes, fwrite blocks when the buffer is full (not returns 0).
        // Returning 0 means an actual I/O error or the reader closed the pipe.
        size_t written = 0;
        size_t total = chunk.size();
        bool writeError = false;
        while (written < total) {
            size_t w = fwrite(chunk.data() + written, sizeof(short), total - written, stdout);
            if (w == 0) {
                StopWriter();
                writeError = true;
                break;
            }
            written += w;
        }

        if (writeError) break;

        fflush(stdout);
    }
}

BOOL WINAPI ConsoleCtrlHandler(DWORD dwCtrlType) {
    if (dwCtrlType == CTRL_C_EVENT || dwCtrlType == CTRL_BREAK_EVENT ||
        dwCtrlType == CTRL_CLOSE_EVENT) {
        StopWriter();
        return TRUE;
    }
    return FALSE;
}

static bool IsStdinBroken(HANDLE hStdin) {
    if (!hStdin || GetFileType(hStdin) != FILE_TYPE_PIPE) return false;
    DWORD avail = 0;
    return !PeekNamedPipe(hStdin, nullptr, 0, nullptr, &avail, nullptr) &&
           GetLastError() == ERROR_BROKEN_PIPE;
}

static std::string FmtSubtype(const GUID& g) {
    if (g == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT) return "float32";
    if (g == KSDATAFORMAT_SUBTYPE_PCM)        return "pcm";
    return "unknown";
}

static bool IsExtensible(const WAVEFORMATEX* wfx) {
    return wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE &&
           wfx->cbSize >= sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
}

static void LogFormat(const char* label, const WAVEFORMATEX* wfx) {
    std::string fmt = IsExtensible(wfx)
        ? FmtSubtype(reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx)->SubFormat)
        : (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT ? "float32" : "pcm");
    LOG_DEBUG(label << ": " << wfx->nSamplesPerSec << " Hz "
              << wfx->nChannels << "ch " << wfx->wBitsPerSample << "-bit " << fmt);
}

static WAVEFORMATEX* AllocFloat32StereoFormat(DWORD sampleRate) {
    auto* wfex = static_cast<WAVEFORMATEXTENSIBLE*>(
        CoTaskMemAlloc(sizeof(WAVEFORMATEXTENSIBLE)));
    if (!wfex) return nullptr;
    *wfex = WAVEFORMATEXTENSIBLE{};
    wfex->Format.wFormatTag           = WAVE_FORMAT_EXTENSIBLE;
    wfex->Format.nChannels            = 2;
    wfex->Format.nSamplesPerSec       = sampleRate;
    wfex->Format.wBitsPerSample       = 32;
    wfex->Format.nBlockAlign          = 8;
    wfex->Format.nAvgBytesPerSec      = sampleRate * 8;
    wfex->Format.cbSize               = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    wfex->Samples.wValidBitsPerSample = 32;
    wfex->dwChannelMask               = SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT;
    wfex->SubFormat                   = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    return &wfex->Format;
}

static inline short ClampFloatToS16(float sample) {
    if (sample > 1.0f) sample = 1.0f;
    else if (sample < -1.0f) sample = -1.0f;
    else if (!(sample >= -1.0f && sample <= 1.0f)) sample = 0.0f; // NaN guard
    return static_cast<short>(sample * 32767.0f);
}

// Convert captured buffer (IEEE float32 or PCM 16/24/32-bit, any channel count) to stereo int16.
// When multichannel audio arrives without WASAPI downmix, mixes center (ch 2) into L and R.
static void ConvertToStereoS16(const BYTE* pData, UINT32 frames, const WAVEFORMATEX* wfx,
                               std::vector<short>& out) {
    WORD nCh = wfx->nChannels;
    if (nCh == 0 || frames == 0) {
        out.assign(static_cast<size_t>(frames) * 2, 0);
        return;
    }

    bool isFloat = (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) ||
                   (IsExtensible(wfx) &&
                    reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx)->SubFormat ==
                        KSDATAFORMAT_SUBTYPE_IEEE_FLOAT);
    WORD bits = wfx->wBitsPerSample;
    constexpr float kCenterGain = 0.7071f;

    out.resize(static_cast<size_t>(frames) * 2);
    if (isFloat && bits == 32) {
        const float* p = reinterpret_cast<const float*>(pData);
        for (UINT32 i = 0; i < frames; ++i) {
            const float* frame = p + static_cast<size_t>(i) * nCh;
            float L = frame[0];
            float R = (nCh > 1) ? frame[1] : frame[0];
            if (nCh > 2) {
                float C = frame[2] * kCenterGain;
                L += C;
                R += C;
            }
            out[static_cast<size_t>(i) * 2]     = ClampFloatToS16(L);
            out[static_cast<size_t>(i) * 2 + 1] = ClampFloatToS16(R);
        }
    } else if (!isFloat && bits == 16) {
        const int16_t* p = reinterpret_cast<const int16_t*>(pData);
        for (UINT32 i = 0; i < frames; ++i) {
            const int16_t* frame = p + static_cast<size_t>(i) * nCh;
            if (nCh <= 2) {
                out[static_cast<size_t>(i) * 2]     = frame[0];
                out[static_cast<size_t>(i) * 2 + 1] = (nCh > 1) ? frame[1] : frame[0];
            } else {
                float C = (frame[2] / 32768.0f) * kCenterGain;
                out[static_cast<size_t>(i) * 2]     = ClampFloatToS16(frame[0] / 32768.0f + C);
                out[static_cast<size_t>(i) * 2 + 1] = ClampFloatToS16(frame[1] / 32768.0f + C);
            }
        }
    } else if (!isFloat && bits == 24) {
        const size_t frameBytes = static_cast<size_t>(nCh) * 3;
        auto read24Norm = [](const BYTE* b) -> float {
            int32_t v = static_cast<int32_t>(
                (static_cast<uint32_t>(b[0]) << 8) |
                (static_cast<uint32_t>(b[1]) << 16) |
                (static_cast<uint32_t>(b[2]) << 24));
            return static_cast<float>(v) / 2147483648.0f;
        };
        for (UINT32 i = 0; i < frames; ++i) {
            const BYTE* frame = pData + static_cast<size_t>(i) * frameBytes;
            float L = read24Norm(frame);
            float R = (nCh > 1) ? read24Norm(frame + 3) : L;
            if (nCh > 2) {
                float C = read24Norm(frame + 6) * kCenterGain;
                L += C;
                R += C;
            }
            out[static_cast<size_t>(i) * 2]     = ClampFloatToS16(L);
            out[static_cast<size_t>(i) * 2 + 1] = ClampFloatToS16(R);
        }
    } else if (!isFloat && bits == 32) {
        const int32_t* p = reinterpret_cast<const int32_t*>(pData);
        for (UINT32 i = 0; i < frames; ++i) {
            const int32_t* frame = p + static_cast<size_t>(i) * nCh;
            float L = static_cast<float>(frame[0]) / 2147483648.0f;
            float R = (nCh > 1) ? static_cast<float>(frame[1]) / 2147483648.0f : L;
            if (nCh > 2) {
                float C = (static_cast<float>(frame[2]) / 2147483648.0f) * kCenterGain;
                L += C;
                R += C;
            }
            out[static_cast<size_t>(i) * 2]     = ClampFloatToS16(L);
            out[static_cast<size_t>(i) * 2 + 1] = ClampFloatToS16(R);
        }
    } else {
        out.assign(static_cast<size_t>(frames) * 2, 0);
    }
}

struct DeviceInfo {
    ComPtr<IMMDevice> device;
    std::string friendlyName; // UTF-8, empty if not found
    WAVEFORMATEX* mixFmt = nullptr; // caller must CoTaskMemFree; null if not found
};

static std::string GetDeviceFriendlyName(IMMDevice* pDev) {
    ComPtr<IPropertyStore> pStore;
    if (FAILED(pDev->OpenPropertyStore(STGM_READ, &pStore))) return "";
    PROPVARIANT pv;
    PropVariantInit(&pv);
    std::string name;
    if (SUCCEEDED(pStore->GetValue(PKEY_Device_FriendlyName, &pv)) &&
        pv.vt == VT_LPWSTR && pv.pwszVal) {
        int len = WideCharToMultiByte(CP_UTF8, 0, pv.pwszVal, -1, nullptr, 0, nullptr, nullptr);
        if (len > 1) {
            name.resize(static_cast<size_t>(len - 1));
            WideCharToMultiByte(CP_UTF8, 0, pv.pwszVal, -1, &name[0], len, nullptr, nullptr);
        }
    }
    PropVariantClear(&pv);
    return name;
}

// Walk active render endpoints via IAudioSessionManager2 to find which endpoint holds an active
// audio session for pid.
static DeviceInfo ResolveDeviceForPid(IMMDeviceEnumerator* pEnum, DWORD pid) {
    ComPtr<IMMDeviceCollection> pCol;
    if (FAILED(pEnum->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &pCol))) return {};
    UINT count = 0;
    pCol->GetCount(&count);

    for (UINT i = 0; i < count; ++i) {
        ComPtr<IMMDevice> pDev;
        if (FAILED(pCol->Item(i, &pDev))) continue;

        LPWSTR rawId = nullptr;
        pDev->GetId(&rawId);
        std::wstring devIdStr = rawId ? rawId : L"";
        if (rawId) CoTaskMemFree(rawId);

        ComPtr<IAudioSessionManager2> pMgr;
        if (FAILED(pDev->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr,
                                  reinterpret_cast<void**>(pMgr.GetAddressOf())))) {
            continue;
        }

        ComPtr<IAudioSessionEnumerator> pSessEnum;
        if (FAILED(pMgr->GetSessionEnumerator(&pSessEnum))) continue;

        int sessCount = 0;
        pSessEnum->GetCount(&sessCount);
        for (int j = 0; j < sessCount; ++j) {
            ComPtr<IAudioSessionControl> pCtrl;
            if (FAILED(pSessEnum->GetSession(j, &pCtrl))) continue;
            ComPtr<IAudioSessionControl2> pCtrl2;
            if (FAILED(pCtrl.As(&pCtrl2))) continue;

            DWORD sessionPid = 0;
            AudioSessionState state = AudioSessionStateInactive;
            pCtrl2->GetProcessId(&sessionPid);
            pCtrl->GetState(&state);

            LOG_DEBUG("endpoint[" << i << "] pid=" << sessionPid
                      << " state=" << state
                      << " dev=" << std::string(devIdStr.begin(), devIdStr.end()));

            if (sessionPid != pid || state != AudioSessionStateActive) continue;

            std::string name = GetDeviceFriendlyName(pDev.Get());
            WAVEFORMATEX* pFmt = nullptr;
            ComPtr<IAudioClient> pTmp;
            if (SUCCEEDED(pDev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                         reinterpret_cast<void**>(pTmp.GetAddressOf())))) {
                pTmp->GetMixFormat(&pFmt);
            }
            LOG_DEBUG("device id: " << std::string(devIdStr.begin(), devIdStr.end()));
            return DeviceInfo{ pDev, name, pFmt };
        }
    }

    return {};
}

// Probe whether an exclusive handle currently owns pDev by attempting a shared-mode Initialize
// without starting playback. Only AUDCLNT_E_DEVICE_IN_USE / AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED
// indicates an exclusive handle; paused shared-mode streams succeed.
static bool IsEndpointInExclusiveMode(IMMDevice* pDev) {
    if (!pDev) return false;
    ComPtr<IAudioClient> pTmp;
    HRESULT hr = pDev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                reinterpret_cast<void**>(pTmp.GetAddressOf()));
    if (FAILED(hr) || !pTmp) return false;

    CoTaskMemGuard mixFmt;
    if (FAILED(pTmp->GetMixFormat(&mixFmt.ptr)) || !mixFmt.ptr) return false;

    hr = pTmp->Initialize(AUDCLNT_SHAREMODE_SHARED, 0, 0, 0, mixFmt.ptr, nullptr);
    return hr == AUDCLNT_E_DEVICE_IN_USE || hr == AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED;
}

static void LogExclusiveModeError(const std::string& friendlyName) {
    LOG_ERROR("audio device \"" << friendlyName
              << "\" is in exclusive mode (AUDCLNT_E_DEVICE_IN_USE). "
              << "Fix: Windows Sound > Properties for \"" << friendlyName
              << "\" > Advanced tab > uncheck "
              << "\"Allow applications to take exclusive control of this device\"");
}

int main(int argc, char** argv) {
    DWORD targetPid = 0;
    DWORD targetSampleRate = 48000;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--pid" && i + 1 < argc) {
            targetPid = std::stoul(argv[++i]);
        } else if (arg == "--sample-rate" && i + 1 < argc) {
            targetSampleRate = std::stoul(argv[++i]);
        } else if (arg == "--debug") {
            g_debug = true;
        }
    }

    if (targetPid == 0) {
        LOG_ERROR("Usage: sessiontape-capture-win.exe --pid <PID> [--sample-rate <RATE>] [--debug]");
        return 1;
    }

    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    if (hStdin == INVALID_HANDLE_VALUE || hStdin == nullptr) hStdin = nullptr;

    _setmode(_fileno(stdout), _O_BINARY);
    setvbuf(stdout, nullptr, _IOFBF, 65536);

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        LOG_ERROR("CoInitializeEx failed: 0x" << std::hex << hr << std::dec);
        return 1;
    }
    CoUninitGuard coinitGuard;

    ComPtr<IMMDeviceEnumerator> pEnum;
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                          __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(pEnum.GetAddressOf()));
    if (FAILED(hr)) {
        LOG_ERROR("CoCreateInstance(MMDeviceEnumerator) failed: 0x" << std::hex << hr << std::dec);
        return 1;
    }

    LOG_INFO("probing active audio session for PID " << targetPid);
    DeviceInfo devInfo = ResolveDeviceForPid(pEnum.Get(), targetPid);
    CoTaskMemGuard endpointFmtGuard;
    endpointFmtGuard.reset(devInfo.mixFmt);

    std::string friendlyName = devInfo.friendlyName.empty() ? "(unknown)" : devInfo.friendlyName;
    if (devInfo.device && IsEndpointInExclusiveMode(devInfo.device.Get())) {
        LogExclusiveModeError(friendlyName);
        return 2;
    }

    DWORD tapRate = endpointFmtGuard.ptr ? endpointFmtGuard.ptr->nSamplesPerSec : 0;
    if (devInfo.device) {
        LOG_INFO("process is rendering to \"" << friendlyName << "\"");
    }

    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = targetPid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;

    PROPVARIANT prop;
    prop.vt = VT_BLOB;
    prop.blob.cbSize = sizeof(params);
    prop.blob.pBlobData = (BYTE*)&params;

    auto handler = Make<CActivateAudioInterfaceCompletionHandler>();
    if (handler->m_initFailed) {
        LOG_ERROR("failed to create activation event");
        return 1;
    }
    ComPtr<IActivateAudioInterfaceAsyncOperation> asyncOp;

    LOG_INFO("activating process loopback for PID " << targetPid);
    hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        __uuidof(IAudioClient),
        &prop,
        handler.Get(),
        &asyncOp
    );

    if (FAILED(hr)) {
        LOG_ERROR("ActivateAudioInterfaceAsync failed: 0x" << std::hex << hr << std::dec);
        return 1;
    }

    if (WaitForSingleObject(handler->m_hActivateCompleted, INFINITE) != WAIT_OBJECT_0) {
        LOG_ERROR("WaitForSingleObject failed");
        return 1;
    }

    if (!handler->m_AudioClient) {
        LOG_ERROR("process loopback activation failed — no IAudioClient");
        return 1;
    }

    ComPtr<IAudioClient> pAudioClient = handler->m_AudioClient;

    // Initialize process loopback directly in stereo Float32 at targetSampleRate so capture
    // starts immediately without waiting for audio activity, and WASAPI handles downmix/resampling.
    CoTaskMemGuard captureFmtGuard;
    captureFmtGuard.reset(AllocFloat32StereoFormat(targetSampleRate));
    if (!captureFmtGuard.ptr) {
        LOG_ERROR("failed to allocate stereo audio format");
        return 1;
    }

    auto isExclusiveHr = [](HRESULT code) {
        return code == AUDCLNT_E_DEVICE_IN_USE || code == AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED;
    };

    hr = pAudioClient->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK |
            AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
        10000000,
        0,
        captureFmtGuard.ptr,
        nullptr
    );

    if (FAILED(hr) && !isExclusiveHr(hr)) {
        hr = pAudioClient->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            10000000,
            0,
            captureFmtGuard.ptr,
            nullptr
        );
    }

    if (FAILED(hr) && !isExclusiveHr(hr) && endpointFmtGuard.ptr) {
        if (tapRate != targetSampleRate) {
            if (hr == AUDCLNT_E_UNSUPPORTED_FORMAT || hr == E_INVALIDARG) {
                std::cerr << "FORMAT_MISMATCH tap=" << tapRate << " requested=" << targetSampleRate << "\n";
                LOG_ERROR("sample rate mismatch: device runs at " << tapRate
                          << " Hz, requested " << targetSampleRate << " Hz (hr=0x"
                          << std::hex << hr << std::dec << ")");
                return 3;
            }
        } else {
            hr = pAudioClient->Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                10000000,
                0,
                endpointFmtGuard.ptr,
                nullptr
            );
            if (SUCCEEDED(hr)) {
                captureFmtGuard.reset(endpointFmtGuard.ptr);
                endpointFmtGuard.ptr = nullptr;
            }
        }
    }

    if (isExclusiveHr(hr)) {
        LogExclusiveModeError(friendlyName);
        return 2;
    }
    if (FAILED(hr)) {
        LOG_ERROR("IAudioClient::Initialize failed: 0x" << std::hex << hr << std::dec);
        return 1;
    }

    LogFormat("capture format", captureFmtGuard.ptr);
    LOG_INFO("capture format: " << captureFmtGuard.ptr->nSamplesPerSec << " Hz "
             << captureFmtGuard.ptr->nChannels << "ch " << captureFmtGuard.ptr->wBitsPerSample << "-bit");

    ComPtr<IAudioCaptureClient> pCaptureClient;
    hr = pAudioClient->GetService(IID_PPV_ARGS(&pCaptureClient));
    if (FAILED(hr)) {
        LOG_ERROR("GetService(IAudioCaptureClient) failed: 0x" << std::hex << hr << std::dec);
        return 1;
    }

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        LOG_ERROR("IAudioClient::Start failed: 0x" << std::hex << hr << std::dec);
        return 1;
    }

    LOG_INFO("capture started");

    std::thread writer(WriterThread);

    UINT32 packetLength = 0;
    std::vector<short> pcmData;
    std::vector<short> silence;
    UINT64 totalChunks = 0;
    UINT64 silentChunks = 0;
    UINT64 nonZeroSamples = 0;
    UINT64 lastReportedChunks = 0;
    const UINT64 DEBUG_REPORT_INTERVAL = 100;
    bool zeroAudioWarnEmitted = false;
    bool exclusiveModeExit = false;

    while (isRunning) {
        Sleep(20);

        if (IsStdinBroken(hStdin)) break;

        hr = pCaptureClient->GetNextPacketSize(&packetLength);
        if (FAILED(hr)) {
            LOG_ERROR("GetNextPacketSize failed: 0x" << std::hex << hr << std::dec);
            break;
        }

        while (packetLength != 0) {
            BYTE* pData;
            UINT32 numFramesAvailable;
            DWORD flags;

            hr = pCaptureClient->GetBuffer(&pData, &numFramesAvailable, &flags, nullptr, nullptr);
            if (FAILED(hr)) break;

            totalChunks++;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                silentChunks++;
                silence.assign(numFramesAvailable * 2, 0);
                {
                    std::lock_guard<std::mutex> lock(queueMutex);
                    if (audioQueue.size() >= MAX_QUEUE_CHUNKS) audioQueue.pop();
                    audioQueue.push(silence);
                }
            } else {
                ConvertToStereoS16(pData, numFramesAvailable, captureFmtGuard.ptr, pcmData);
                for (short s : pcmData) {
                    if (s != 0) nonZeroSamples++;
                }
                {
                    std::lock_guard<std::mutex> lock(queueMutex);
                    if (audioQueue.size() >= MAX_QUEUE_CHUNKS) audioQueue.pop();
                    audioQueue.push(pcmData);
                }
            }

            queueCV.notify_one();

            hr = pCaptureClient->ReleaseBuffer(numFramesAvailable);
            if (FAILED(hr)) break;

            hr = pCaptureClient->GetNextPacketSize(&packetLength);
            if (FAILED(hr)) break;
        }

        if (totalChunks - lastReportedChunks >= DEBUG_REPORT_INTERVAL) {
            lastReportedChunks = totalChunks;
            UINT64 active = totalChunks - silentChunks;
            size_t queueDepth = 0;
            {
                std::lock_guard<std::mutex> lock(queueMutex);
                queueDepth = audioQueue.size();
            }
            LOG_DEBUG("chunks=" << totalChunks
                      << " silent=" << silentChunks
                      << " active=" << active
                      << " nonzero_samples=" << nonZeroSamples
                      << " queue=" << queueDepth);

            if (active >= 200 && nonZeroSamples == 0) {
                if (!devInfo.device) {
                    DeviceInfo refreshed = ResolveDeviceForPid(pEnum.Get(), targetPid);
                    if (refreshed.mixFmt) CoTaskMemFree(refreshed.mixFmt);
                    if (refreshed.device) {
                        devInfo.device = refreshed.device;
                        if (!refreshed.friendlyName.empty()) {
                            friendlyName = refreshed.friendlyName;
                        }
                    }
                }

                if (devInfo.device && IsEndpointInExclusiveMode(devInfo.device.Get())) {
                    LogExclusiveModeError(friendlyName);
                    exclusiveModeExit = true;
                    StopWriter();
                    break;
                }

                if (!zeroAudioWarnEmitted) {
                    zeroAudioWarnEmitted = true;
                    LOG_WARN("receiving active buffers but all samples are zero on \""
                             << friendlyName
                             << "\"; audio may be paused or device may be in exclusive mode");
                }
            }
        }
    }

    StopWriter();
    if (writer.joinable()) {
        writer.join();
    }

    pAudioClient->Stop();

    return exclusiveModeExit ? 2 : 0;
}
