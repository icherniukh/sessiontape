#define NOMINMAX
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
    DWORD                 TargetProcessId;
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

#ifndef AUDCLNT_E_DEVICE_INVALIDATED
#define AUDCLNT_E_DEVICE_INVALIDATED ((HRESULT)0x88890004L)
#endif
#ifndef AUDCLNT_E_UNSUPPORTED_FORMAT
#define AUDCLNT_E_UNSUPPORTED_FORMAT ((HRESULT)0x88890008L)
#endif
#ifndef AUDCLNT_E_DEVICE_IN_USE
#define AUDCLNT_E_DEVICE_IN_USE ((HRESULT)0x8889000AL)
#endif
#ifndef AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED
#define AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED ((HRESULT)0x8889000EL)
#endif
#ifndef AUDCLNT_E_SERVICE_NOT_RUNNING
#define AUDCLNT_E_SERVICE_NOT_RUNNING ((HRESULT)0x88890010L)
#endif
#ifndef AUDCLNT_E_RESOURCES_INVALIDATED
#define AUDCLNT_E_RESOURCES_INVALIDATED ((HRESULT)0x88890026L)
#endif
#ifndef AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
#define AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM 0x80000000
#endif
#ifndef AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
#define AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY 0x08000000
#endif

#include <wrl/implements.h>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <fcntl.h>
#include <io.h>
#include <iostream>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

#include "pcm_convert.h"

using namespace Microsoft::WRL;

static bool g_debug = false;

#define LOG_INFO(msg)  do { std::cerr << "INFO: "    << msg << "\n" << std::flush; } while (0)
#define LOG_WARN(msg)  do { std::cerr << "WARNING: " << msg << "\n" << std::flush; } while (0)
#define LOG_ERROR(msg) do { std::cerr << "ERROR: "   << msg << "\n" << std::flush; } while (0)
#define LOG_DEBUG(msg) do { if (g_debug) { std::cerr << "DEBUG: " << msg << "\n" << std::flush; } } while (0)

struct CoUninitGuard {
    ~CoUninitGuard() { CoUninitialize(); }
};

struct HandleGuard {
    HANDLE handle = nullptr;
    ~HandleGuard() {
        if (handle && handle != INVALID_HANDLE_VALUE) {
            CloseHandle(handle);
        }
    }
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

static std::string WideToUtf8(const wchar_t* wstr) {
    if (!wstr || *wstr == L'\0') return "";
    const int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 1) return "";
    std::string out(static_cast<size_t>(len - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, wstr, -1, &out[0], len, nullptr, nullptr);
    return out;
}

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
        HRESULT hrActivate = E_FAIL;
        ComPtr<IUnknown> punkAudioClient;

        const HRESULT hr = operation->GetActivateResult(&hrActivate, &punkAudioClient);
        m_hrOperation = hr;
        m_hrActivate = hrActivate;
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate) && punkAudioClient) {
            punkAudioClient.As(&m_AudioClient);
        }

        if (m_hActivateCompleted) SetEvent(m_hActivateCompleted);
        return S_OK;
    }

    ComPtr<IAudioClient> m_AudioClient;
    HANDLE m_hActivateCompleted = nullptr;
    HRESULT m_hrOperation = E_FAIL;
    HRESULT m_hrActivate = E_FAIL;
    bool m_initFailed = false;
};

// Global state for IPC writer thread
std::queue<std::vector<int16_t>> audioQueue;
std::mutex queueMutex;
std::condition_variable queueCV;
std::atomic<bool> isRunning{true};
std::atomic<uint64_t> g_bytesWritten{0};
constexpr size_t MAX_QUEUE_CHUNKS = 5000; // ~50-100 seconds of audio to bound memory
constexpr DWORD ACTIVATION_TIMEOUT_MS = 10000;
constexpr ULONGLONG METRICS_INTERVAL_MS = 1000;
constexpr ULONGLONG SILENT_STARTUP_WARN_MS = 60000;

static void StopWriter() {
    {
        std::lock_guard<std::mutex> lock(queueMutex);
        isRunning = false;
    }
    queueCV.notify_all();
}

void WriterThread() {
    while (true) {
        std::vector<int16_t> chunk;
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
        const size_t total = chunk.size();
        bool writeError = false;
        while (written < total) {
            const size_t w = fwrite(chunk.data() + written, sizeof(int16_t), total - written, stdout);
            if (w == 0) {
                StopWriter();
                writeError = true;
                break;
            }
            written += w;
            g_bytesWritten.fetch_add(w * sizeof(int16_t), std::memory_order_relaxed);
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

static PcmInputFormat DescribeInputFormat(const WAVEFORMATEX* wfx) {
    PcmInputFormat fmt{};
    if (!wfx) return fmt;
    fmt.channels = wfx->nChannels;
    fmt.bits_per_sample = wfx->wBitsPerSample;

    if (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT ||
        (IsExtensible(wfx) &&
         reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx)->SubFormat ==
             KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)) {
        fmt.tag = PcmFormatTag::kFloat32;
    } else if (wfx->wFormatTag == WAVE_FORMAT_PCM ||
               (IsExtensible(wfx) &&
                reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx)->SubFormat ==
                    KSDATAFORMAT_SUBTYPE_PCM)) {
        fmt.tag = PcmFormatTag::kPcmInt;
    } else {
        fmt.tag = PcmFormatTag::kUnknown;
    }
    return fmt;
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
        name = WideToUtf8(pv.pwszVal);
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
        std::string devIdUtf8 = rawId ? WideToUtf8(rawId) : "";
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
                      << " dev=" << devIdUtf8);

            if (sessionPid != pid || state != AudioSessionStateActive) continue;

            std::string name = GetDeviceFriendlyName(pDev.Get());
            WAVEFORMATEX* pFmt = nullptr;
            ComPtr<IAudioClient> pTmp;
            if (SUCCEEDED(pDev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                         reinterpret_cast<void**>(pTmp.GetAddressOf())))) {
                pTmp->GetMixFormat(&pFmt);
            }
            LOG_DEBUG("device id: " << devIdUtf8);
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

static bool IsExclusiveModeHr(HRESULT hr) {
    return hr == AUDCLNT_E_DEVICE_IN_USE || hr == AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED;
}

static bool IsRestartableCaptureHr(HRESULT hr) {
    return hr == AUDCLNT_E_DEVICE_INVALIDATED ||
           hr == AUDCLNT_E_RESOURCES_INVALIDATED ||
           hr == AUDCLNT_E_SERVICE_NOT_RUNNING;
}

static bool IsUnsupportedLoopbackHr(HRESULT hr) {
    return hr == E_NOTIMPL || hr == REGDB_E_CLASSNOTREG || hr == E_NOINTERFACE;
}

int main(int argc, char** argv) {
    CaptureCliArgs cliArgs;
    std::string parseError;
    if (!ParseCaptureArgs(argc, argv, cliArgs, parseError)) {
        LOG_ERROR(parseError << ". Usage: sessiontape-capture-win.exe --pid <PID> "
                  "[--sample-rate <RATE>] [--debug] [--no-metrics]");
        return kExitConfigError;
    }

    const DWORD targetPid = cliArgs.pid;
    const DWORD targetSampleRate = cliArgs.sample_rate;
    g_debug = cliArgs.debug;
    const bool emitMetrics = cliArgs.metrics;

    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    if (hStdin == INVALID_HANDLE_VALUE || hStdin == nullptr) hStdin = nullptr;

    // Open a handle to detect immediate target-process exit; tolerate ACCESS_DENIED if elevated.
    HandleGuard targetProcGuard;
    targetProcGuard.handle = OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, targetPid
    );
    if (!targetProcGuard.handle && GetLastError() == ERROR_INVALID_PARAMETER) {
        LOG_ERROR("target process PID " << targetPid << " is not running");
        return kExitGeneralError;
    }

    _setmode(_fileno(stdout), _O_BINARY);
    setvbuf(stdout, nullptr, _IOFBF, 65536);

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        LOG_ERROR("CoInitializeEx failed: 0x" << std::hex << hr << std::dec);
        return kExitGeneralError;
    }
    CoUninitGuard coinitGuard;

    ComPtr<IMMDeviceEnumerator> pEnum;
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                          __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(pEnum.GetAddressOf()));
    if (FAILED(hr)) {
        LOG_ERROR("CoCreateInstance(MMDeviceEnumerator) failed: 0x" << std::hex << hr << std::dec);
        return kExitGeneralError;
    }

    LOG_INFO("probing active audio session for PID " << targetPid);
    DeviceInfo devInfo = ResolveDeviceForPid(pEnum.Get(), targetPid);
    CoTaskMemGuard endpointFmtGuard;
    endpointFmtGuard.reset(devInfo.mixFmt);

    std::string friendlyName = devInfo.friendlyName.empty() ? "(unknown)" : devInfo.friendlyName;
    if (devInfo.device && IsEndpointInExclusiveMode(devInfo.device.Get())) {
        LogExclusiveModeError(friendlyName);
        return kExitConfigError;
    }

    const DWORD tapRate = endpointFmtGuard.ptr ? endpointFmtGuard.ptr->nSamplesPerSec : 0;
    if (devInfo.device) {
        LOG_INFO("process is rendering to \"" << friendlyName << "\"");
    }

    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = targetPid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;

    PROPVARIANT prop = {};
    prop.vt = VT_BLOB;
    prop.blob.cbSize = sizeof(params);
    prop.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    auto handler = Make<CActivateAudioInterfaceCompletionHandler>();
    if (handler->m_initFailed) {
        LOG_ERROR("failed to create activation event");
        return kExitGeneralError;
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
        if (IsUnsupportedLoopbackHr(hr)) {
            LOG_ERROR("Windows per-process audio loopback is not supported on this OS build "
                      "(requires Windows 10 Build 19041+ / Windows 11, hr=0x"
                      << std::hex << hr << std::dec << ")");
            return kExitConfigError;
        }
        LOG_ERROR("ActivateAudioInterfaceAsync failed: 0x" << std::hex << hr << std::dec);
        return kExitGeneralError;
    }

    const DWORD waitStatus = WaitForSingleObject(handler->m_hActivateCompleted, ACTIVATION_TIMEOUT_MS);
    if (waitStatus == WAIT_TIMEOUT) {
        LOG_ERROR("process loopback activation timed out after " << ACTIVATION_TIMEOUT_MS << " ms");
        return kExitGeneralError;
    }
    if (waitStatus != WAIT_OBJECT_0) {
        LOG_ERROR("WaitForSingleObject failed: " << GetLastError());
        return kExitGeneralError;
    }

    if (!handler->m_AudioClient) {
        if (IsUnsupportedLoopbackHr(handler->m_hrActivate)) {
            LOG_ERROR("Windows per-process audio loopback is not supported on this OS build "
                      "(requires Windows 10 Build 19041+ / Windows 11, hr=0x"
                      << std::hex << handler->m_hrActivate << std::dec << ")");
            return kExitConfigError;
        }
        LOG_ERROR("process loopback activation failed (op=0x"
                  << std::hex << handler->m_hrOperation
                  << " activate=0x" << handler->m_hrActivate << std::dec << ")");
        return kExitGeneralError;
    }

    ComPtr<IAudioClient> pAudioClient = handler->m_AudioClient;

    // Initialize process loopback directly in stereo Float32 at targetSampleRate so capture
    // starts immediately without waiting for audio activity, and WASAPI handles downmix/resampling.
    CoTaskMemGuard captureFmtGuard;
    captureFmtGuard.reset(AllocFloat32StereoFormat(targetSampleRate));
    if (!captureFmtGuard.ptr) {
        LOG_ERROR("failed to allocate stereo audio format");
        return kExitGeneralError;
    }

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

    if (FAILED(hr) && !IsExclusiveModeHr(hr)) {
        hr = pAudioClient->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            10000000,
            0,
            captureFmtGuard.ptr,
            nullptr
        );
    }

    // If WASAPI rejected targetSampleRate without AUTOCONVERTPCM, initialize directly at the
    // endpoint's native mix format and notify Python to adopt that sample rate.
    if (FAILED(hr) && !IsExclusiveModeHr(hr) && endpointFmtGuard.ptr) {
        const HRESULT fallbackHr = pAudioClient->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            10000000,
            0,
            endpointFmtGuard.ptr,
            nullptr
        );
        if (SUCCEEDED(fallbackHr)) {
            hr = fallbackHr;
            captureFmtGuard.reset(endpointFmtGuard.ptr);
            endpointFmtGuard.ptr = nullptr;
        } else if (tapRate != 0 && tapRate != targetSampleRate &&
                   (hr == AUDCLNT_E_UNSUPPORTED_FORMAT || hr == E_INVALIDARG)) {
            std::cerr << "FORMAT_MISMATCH tap=" << tapRate
                      << " requested=" << targetSampleRate << "\n" << std::flush;
            LOG_ERROR("sample rate mismatch: device runs at " << tapRate
                      << " Hz, requested " << targetSampleRate << " Hz (hr=0x"
                      << std::hex << hr << std::dec << ")");
            return kExitFormatMismatch;
        }
    }

    if (IsExclusiveModeHr(hr)) {
        LogExclusiveModeError(friendlyName);
        return kExitConfigError;
    }
    if (FAILED(hr)) {
        LOG_ERROR("IAudioClient::Initialize failed: 0x" << std::hex << hr << std::dec);
        return kExitGeneralError;
    }

    const DWORD activeCaptureRate = captureFmtGuard.ptr->nSamplesPerSec;
    const PcmInputFormat inputFmt = DescribeInputFormat(captureFmtGuard.ptr);
    if (inputFmt.tag == PcmFormatTag::kUnknown) {
        LOG_ERROR("unsupported WASAPI sample format (tag=0x"
                  << std::hex << captureFmtGuard.ptr->wFormatTag << std::dec
                  << ", bits=" << captureFmtGuard.ptr->wBitsPerSample << ")");
        return kExitGeneralError;
    }

    LogFormat("capture format", captureFmtGuard.ptr);
    LOG_INFO("capture format: " << activeCaptureRate << " Hz "
             << captureFmtGuard.ptr->nChannels << "ch "
             << captureFmtGuard.ptr->wBitsPerSample << "-bit");

    ComPtr<IAudioCaptureClient> pCaptureClient;
    hr = pAudioClient->GetService(IID_PPV_ARGS(&pCaptureClient));
    if (FAILED(hr)) {
        LOG_ERROR("GetService(IAudioCaptureClient) failed: 0x" << std::hex << hr << std::dec);
        return kExitGeneralError;
    }

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        if (IsExclusiveModeHr(hr)) {
            LogExclusiveModeError(friendlyName);
            return kExitConfigError;
        }
        LOG_ERROR("IAudioClient::Start failed: 0x" << std::hex << hr << std::dec);
        return kExitGeneralError;
    }

    if (activeCaptureRate != targetSampleRate) {
        std::cerr << "CAPTURE_FORMAT rate=" << activeCaptureRate << " channels=2\n" << std::flush;
    }

    LOG_INFO("capture started");

    std::thread writer(WriterThread);

    UINT32 packetLength = 0;
    UINT64 totalChunks = 0;
    UINT64 windowCallbacks = 0;
    UINT64 droppedChunks = 0;
    UINT64 windowDrops = 0;
    UINT64 windowDiscontinuities = 0;
    UINT64 silentChunks = 0;
    UINT64 nonZeroSamples = 0;
    UINT64 lastReportedChunks = 0;
    constexpr UINT64 DEBUG_REPORT_INTERVAL = 100;
    const ULONGLONG startTick = GetTickCount64();
    ULONGLONG lastMetricsTick = startTick;
    bool zeroAudioWarnEmitted = false;
    bool silentStartupWarnEmitted = false;
    bool exclusiveModeExit = false;
    HRESULT captureErrorHr = S_OK;

    while (isRunning) {
        Sleep(20);

        if (IsStdinBroken(hStdin)) break;
        if (targetProcGuard.handle &&
            WaitForSingleObject(targetProcGuard.handle, 0) == WAIT_OBJECT_0) {
            LOG_INFO("target process PID " << targetPid << " exited");
            break;
        }

        hr = pCaptureClient->GetNextPacketSize(&packetLength);
        if (FAILED(hr)) {
            LOG_ERROR("GetNextPacketSize failed: 0x" << std::hex << hr << std::dec);
            captureErrorHr = hr;
            break;
        }

        while (packetLength != 0) {
            BYTE* pData = nullptr;
            UINT32 numFramesAvailable = 0;
            DWORD flags = 0;

            hr = pCaptureClient->GetBuffer(&pData, &numFramesAvailable, &flags, nullptr, nullptr);
            if (FAILED(hr)) {
                LOG_ERROR("GetBuffer failed: 0x" << std::hex << hr << std::dec);
                captureErrorHr = hr;
                break;
            }

            totalChunks++;
            windowCallbacks++;
            if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) {
                windowDiscontinuities++;
            }

            std::vector<int16_t> outChunk;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                silentChunks++;
                outChunk.assign(static_cast<size_t>(numFramesAvailable) * 2, 0);
            } else {
                nonZeroSamples += ConvertToStereoS16(
                    pData, numFramesAvailable, inputFmt, outChunk
                );
            }

            hr = pCaptureClient->ReleaseBuffer(numFramesAvailable);
            if (FAILED(hr)) {
                LOG_ERROR("ReleaseBuffer failed: 0x" << std::hex << hr << std::dec);
                captureErrorHr = hr;
                break;
            }

            if (!outChunk.empty()) {
                std::lock_guard<std::mutex> lock(queueMutex);
                if (audioQueue.size() >= MAX_QUEUE_CHUNKS) {
                    audioQueue.pop();
                    droppedChunks++;
                    windowDrops++;
                }
                audioQueue.push(std::move(outChunk));
            }
            queueCV.notify_one();

            hr = pCaptureClient->GetNextPacketSize(&packetLength);
            if (FAILED(hr)) {
                LOG_ERROR("GetNextPacketSize failed: 0x" << std::hex << hr << std::dec);
                captureErrorHr = hr;
                break;
            }
        }

        if (FAILED(captureErrorHr)) {
            break;
        }

        const ULONGLONG nowTick = GetTickCount64();
        if (emitMetrics && nowTick - lastMetricsTick >= METRICS_INTERVAL_MS) {
            lastMetricsTick = nowTick;
            const uint64_t bytesNow = g_bytesWritten.load(std::memory_order_relaxed);
            LOG_INFO(FormatCaptureMetricsLine(
                windowDrops, windowCallbacks, bytesNow, windowDiscontinuities
            ));
            windowDrops = 0;
            windowCallbacks = 0;
            windowDiscontinuities = 0;
        }

        if (!silentStartupWarnEmitted && nonZeroSamples == 0 &&
            nowTick - startTick >= SILENT_STARTUP_WARN_MS) {
            silentStartupWarnEmitted = true;
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

            if (!devInfo.device) {
                LOG_WARN("no active WASAPI audio session detected for PID " << targetPid
                         << " after 60s. If Rekordbox is playing through an ASIO driver, "
                         << "enable 'PC Master Out' in Rekordbox Audio settings or select "
                         << "a Windows shared-mode output so SessionTape can capture it.");
            }
        }

        if (totalChunks - lastReportedChunks >= DEBUG_REPORT_INTERVAL) {
            lastReportedChunks = totalChunks;
            const UINT64 active = totalChunks - silentChunks;
            size_t queueDepth = 0;
            {
                std::lock_guard<std::mutex> lock(queueMutex);
                queueDepth = audioQueue.size();
            }
            LOG_DEBUG("chunks=" << totalChunks
                      << " silent=" << silentChunks
                      << " active=" << active
                      << " drops=" << droppedChunks
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

    if (exclusiveModeExit || IsExclusiveModeHr(captureErrorHr)) {
        if (!exclusiveModeExit) {
            LogExclusiveModeError(friendlyName);
        }
        return kExitConfigError;
    }
    if (IsRestartableCaptureHr(captureErrorHr)) {
        LOG_WARN("WASAPI capture endpoint invalidated or audio service restarted (hr=0x"
                 << std::hex << captureErrorHr << std::dec
                 << "); exiting for daemon restart");
        return kExitDeviceOrStateChanged;
    }
    if (FAILED(captureErrorHr)) {
        return kExitGeneralError;
    }
    return 0;
}
