#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
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

#include <wrl/implements.h>
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

#define LOG_INFO(msg)  do { std::cerr << "INFO: "    << msg << "\n"; } while(0)
#define LOG_WARN(msg)  do { std::cerr << "WARNING: " << msg << "\n"; } while(0)
#define LOG_ERROR(msg) do { std::cerr << "ERROR: "   << msg << "\n"; } while(0)
#define LOG_DEBUG(msg) do { if (g_debug) { std::cerr << "DEBUG: " << msg << "\n"; } } while(0)

// ── Writer thread ─────────────────────────────────────────────────────────────

std::queue<std::vector<short>> audioQueue;
std::mutex queueMutex;
std::condition_variable queueCV;
std::atomic<bool> isRunning{true};
const size_t MAX_QUEUE_CHUNKS = 5000;

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
        size_t written = 0, total = chunk.size();
        while (written < total) {
            size_t w = fwrite(chunk.data() + written, sizeof(short), total - written, stdout);
            if (w == 0) {
                isRunning = false;
                queueCV.notify_all();
                goto done;
            }
            written += w;
        }
        fflush(stdout);
    }
done:;
}

BOOL WINAPI ConsoleCtrlHandler(DWORD dwCtrlType) {
    if (dwCtrlType == CTRL_C_EVENT || dwCtrlType == CTRL_BREAK_EVENT ||
        dwCtrlType == CTRL_CLOSE_EVENT) {
        isRunning = false;
        queueCV.notify_all();
        return TRUE;
    }
    return FALSE;
}

// ── Format helpers ────────────────────────────────────────────────────────────

static std::string FmtSubtype(const GUID& g) {
    if (g == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT) return "float32";
    if (g == KSDATAFORMAT_SUBTYPE_PCM)        return "pcm";
    return "unknown";
}

static void LogFormat(const char* label, const WAVEFORMATEX* wfx) {
    std::string fmt = (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE)
        ? FmtSubtype(reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx)->SubFormat)
        : (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT ? "float32" : "pcm");
    LOG_DEBUG(label << ": " << wfx->nSamplesPerSec << " Hz "
              << wfx->nChannels << "ch " << wfx->wBitsPerSample << "-bit " << fmt);
}

// Convert a captured buffer (float32 or int16, any channel count) to stereo int16.
static void ConvertToStereoS16(const BYTE* pData, UINT32 frames, const WAVEFORMATEX* wfx,
                                 std::vector<short>& out) {
    WORD nCh = wfx->nChannels;
    bool isFloat = (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) ||
                   (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE &&
                    reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx)->SubFormat
                        == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT);

    out.resize(frames * 2);
    for (UINT32 i = 0; i < frames; ++i) {
        float L = 0.0f, R = 0.0f;
        if (isFloat) {
            const float* p = reinterpret_cast<const float*>(pData) + i * nCh;
            L = p[0];
            R = (nCh > 1) ? p[1] : p[0];
        } else {
            const short* p = reinterpret_cast<const short*>(pData) + i * nCh;
            L = p[0] / 32768.0f;
            R = (nCh > 1) ? p[1] / 32768.0f : p[0] / 32768.0f;
        }
        L = L > 1.0f ? 1.0f : (L < -1.0f ? -1.0f : L);
        R = R > 1.0f ? 1.0f : (R < -1.0f ? -1.0f : R);
        out[i * 2]     = static_cast<short>(L * 32767.0f);
        out[i * 2 + 1] = static_cast<short>(R * 32767.0f);
    }
}

// ── Device resolution ─────────────────────────────────────────────────────────

struct DeviceInfo {
    std::string  friendlyName;  // UTF-8, empty if not found
    WAVEFORMATEX* mixFmt;       // caller must CoTaskMemFree; null if not found
};

// Single-attempt scan: walk all active render endpoints and find the one with
// an active session for pid. Returns DeviceInfo{} if not found.
static DeviceInfo ResolveDeviceForPid(IMMDeviceEnumerator* pEnum, DWORD pid) {
    auto getFriendlyName = [](IMMDevice* pDev) -> std::string {
        ComPtr<IPropertyStore> pStore;
        if (FAILED(pDev->OpenPropertyStore(STGM_READ, &pStore))) return "";
        PROPVARIANT pv; PropVariantInit(&pv);
        std::string name;
        if (SUCCEEDED(pStore->GetValue(PKEY_Device_FriendlyName, &pv))
                && pv.vt == VT_LPWSTR && pv.pwszVal) {
            int len = WideCharToMultiByte(CP_UTF8, 0, pv.pwszVal, -1,
                                          nullptr, 0, nullptr, nullptr);
            name.resize(len - 1);
            WideCharToMultiByte(CP_UTF8, 0, pv.pwszVal, -1,
                                &name[0], len, nullptr, nullptr);
        }
        PropVariantClear(&pv);
        return name;
    };

    ComPtr<IMMDeviceCollection> pCol;
    if (FAILED(pEnum->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &pCol))) return {};
    UINT count = 0; pCol->GetCount(&count);

    for (UINT i = 0; i < count; ++i) {
        ComPtr<IMMDevice> pDev;
        if (FAILED(pCol->Item(i, &pDev))) continue;

        LPWSTR rawId = nullptr; pDev->GetId(&rawId);
        std::wstring devIdStr = rawId ? rawId : L"";
        if (rawId) CoTaskMemFree(rawId);

        ComPtr<IAudioSessionManager2> pMgr;
        if (FAILED(pDev->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL,
                                   nullptr, reinterpret_cast<void**>(pMgr.GetAddressOf()))))
            continue;

        ComPtr<IAudioSessionEnumerator> pSessEnum;
        if (FAILED(pMgr->GetSessionEnumerator(&pSessEnum))) continue;

        int sessCount = 0; pSessEnum->GetCount(&sessCount);
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

            std::string name = getFriendlyName(pDev.Get());
            WAVEFORMATEX* pFmt = nullptr;
            ComPtr<IAudioClient> pTmp;
            if (SUCCEEDED(pDev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                          reinterpret_cast<void**>(pTmp.GetAddressOf()))))
                pTmp->GetMixFormat(&pFmt);

            LOG_DEBUG("device id: " << std::string(devIdStr.begin(), devIdStr.end()));
            return DeviceInfo{ name, pFmt };
        }
    }
    return {};
}

// ── Process loopback activation (async) ──────────────────────────────────────

class CActivateAudioInterfaceCompletionHandler :
    public RuntimeClass<RuntimeClassFlags<ClassicCom>, IActivateAudioInterfaceCompletionHandler, FtmBase>
{
public:
    ComPtr<IAudioClient> m_AudioClient;
    HANDLE m_hDone = CreateEvent(nullptr, FALSE, FALSE, nullptr);

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* op) {
        HRESULT hrActivate = S_OK;
        IUnknown* pUnk = nullptr;
        HRESULT hr = op->GetActivateResult(&hrActivate, &pUnk);
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate) && pUnk)
            pUnk->QueryInterface(IID_PPV_ARGS(&m_AudioClient));
        SetEvent(m_hDone);
        return S_OK;
    }
};

// ── Main ──────────────────────────────────────────────────────────────────────

int main(int argc, char** argv) {
    DWORD targetPid = 0;
    DWORD targetSampleRate = 48000;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--pid" && i + 1 < argc)              targetPid = std::stoul(argv[++i]);
        else if (arg == "--sample-rate" && i + 1 < argc) targetSampleRate = std::stoul(argv[++i]);
        else if (arg == "--debug")                        g_debug = true;
    }

    if (targetPid == 0) {
        LOG_ERROR("usage: --pid <PID> [--sample-rate <RATE>] [--debug]");
        return 1;
    }

    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    if (hStdin == INVALID_HANDLE_VALUE || hStdin == nullptr) hStdin = nullptr;

    _setmode(_fileno(stdout), _O_BINARY);
    char stdoutBuffer[65536];
    setvbuf(stdout, stdoutBuffer, _IOFBF, sizeof(stdoutBuffer));

    struct CoUninitGuard { ~CoUninitGuard() { CoUninitialize(); } };
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) { LOG_ERROR("CoInitializeEx failed: 0x" << std::hex << hr); return 1; }
    CoUninitGuard coinitGuard;

    // ── Step 1: find the render endpoint for this PID to get its mix format ──
    ComPtr<IMMDeviceEnumerator> pEnum;
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                          __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(pEnum.GetAddressOf()));
    if (FAILED(hr)) { LOG_ERROR("CoCreateInstance(MMDeviceEnumerator) failed: 0x" << std::hex << hr); return 1; }

    LOG_INFO("searching for active audio session for PID " << targetPid);
    DeviceInfo devInfo;
    for (int attempt = 0; attempt < 10 && devInfo.friendlyName.empty(); ++attempt) {
        if (attempt > 0) {
            LOG_DEBUG("no session found, retry " << attempt << "/10");
            // Sleep in short increments so stdin-close is detected promptly.
            for (int ms = 0; ms < 2000; ms += 50) {
                Sleep(50);
                if (hStdin && GetFileType(hStdin) == FILE_TYPE_PIPE) {
                    DWORD avail = 0;
                    if (!PeekNamedPipe(hStdin, nullptr, 0, nullptr, &avail, nullptr) &&
                        GetLastError() == ERROR_BROKEN_PIPE) {
                        CoTaskMemFree(devInfo.mixFmt);
                        return 0;
                    }
                }
            }
        }
        devInfo = ResolveDeviceForPid(pEnum.Get(), targetPid);
    }

    WAVEFORMATEX* pMixFmt = devInfo.mixFmt;
    std::string friendlyName = devInfo.friendlyName.empty() ? "(unknown)" : devInfo.friendlyName;

    if (pMixFmt) {
        LOG_INFO("process is rendering to \"" << friendlyName << "\"");
    } else {
        LOG_WARN("no active audio session found for PID " << targetPid
                 << " after 10 attempts; process may not have started audio yet");
    }

    // Fall back to stereo float32 if device format could not be determined.
    if (!pMixFmt) {
        LOG_WARN("could not determine mix format; falling back to 2ch float32 at " << targetSampleRate << " Hz");
        auto* wfex = static_cast<WAVEFORMATEXTENSIBLE*>(
            CoTaskMemAlloc(sizeof(WAVEFORMATEXTENSIBLE)));
        *wfex = WAVEFORMATEXTENSIBLE{};
        wfex->Format.wFormatTag           = WAVE_FORMAT_EXTENSIBLE;
        wfex->Format.nChannels            = 2;
        wfex->Format.nSamplesPerSec       = targetSampleRate;
        wfex->Format.wBitsPerSample       = 32;
        wfex->Format.nBlockAlign          = 8;
        wfex->Format.nAvgBytesPerSec      = targetSampleRate * 8;
        wfex->Format.cbSize               = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
        wfex->Samples.wValidBitsPerSample = 32;
        wfex->dwChannelMask               = SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT;
        wfex->SubFormat                   = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
        pMixFmt = &wfex->Format;
    }

    // ── Step 2: activate process loopback ────────────────────────────────────
    AUDIOCLIENT_ACTIVATION_PARAMS loopbackParams = {};
    loopbackParams.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    loopbackParams.ProcessLoopbackParams.TargetProcessId = targetPid;
    loopbackParams.ProcessLoopbackParams.ProcessLoopbackMode =
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;

    PROPVARIANT prop;
    prop.vt = VT_BLOB;
    prop.blob.cbSize   = sizeof(loopbackParams);
    prop.blob.pBlobData = (BYTE*)&loopbackParams;

    auto handler = Make<CActivateAudioInterfaceCompletionHandler>();
    ComPtr<IActivateAudioInterfaceAsyncOperation> asyncOp;

    LOG_INFO("activating process loopback for PID " << targetPid);
    hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
                                      __uuidof(IAudioClient), &prop,
                                      handler.Get(), &asyncOp);
    if (FAILED(hr)) {
        LOG_ERROR("ActivateAudioInterfaceAsync failed: 0x" << std::hex << hr);
        CoTaskMemFree(pMixFmt);
        return 1;
    }

    WaitForSingleObject(handler->m_hDone, INFINITE);

    if (!handler->m_AudioClient) {
        LOG_ERROR("process loopback activation failed — no IAudioClient");
        CoTaskMemFree(pMixFmt);
        return 1;
    }

    ComPtr<IAudioClient> pAudioClient = handler->m_AudioClient;

    // GetMixFormat is not implemented on process loopback clients (E_NOTIMPL).
    // Validate with IsFormatSupported instead and fall back to 2ch if needed.
    {
        WAVEFORMATEX* pClosest = nullptr;
        HRESULT hrFmt = pAudioClient->IsFormatSupported(
            AUDCLNT_SHAREMODE_SHARED, pMixFmt, &pClosest);
        if (hrFmt == S_FALSE && pClosest) {
            LOG_WARN("requested format not supported; using closest match");
            CoTaskMemFree(pMixFmt);
            pMixFmt = pClosest;
        } else if (FAILED(hrFmt)) {
            LOG_WARN("IsFormatSupported returned 0x" << std::hex << hrFmt << std::dec
                     << "; proceeding with device format anyway");
        } else {
            if (pClosest) CoTaskMemFree(pClosest);
        }
    }

    LogFormat("capture format", pMixFmt);
    LOG_INFO("capture format: " << pMixFmt->nSamplesPerSec << " Hz "
             << pMixFmt->nChannels << "ch " << pMixFmt->wBitsPerSample << "-bit");

    if (pMixFmt->nSamplesPerSec != targetSampleRate) {
        LOG_WARN("capture rate is " << pMixFmt->nSamplesPerSec
                 << " Hz but --sample-rate=" << targetSampleRate
                 << "; outputting at capture rate");
    }

    hr = pAudioClient->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK,
        10000000, // 1-second buffer
        0,
        pMixFmt,
        nullptr
    );
    if (FAILED(hr)) {
        LOG_ERROR("IAudioClient Initialize failed: 0x" << std::hex << hr);
        CoTaskMemFree(pMixFmt);
        return 1;
    }

    ComPtr<IAudioCaptureClient> pCaptureClient;
    hr = pAudioClient->GetService(IID_PPV_ARGS(&pCaptureClient));
    if (FAILED(hr)) {
        LOG_ERROR("GetService(IAudioCaptureClient) failed: 0x" << std::hex << hr);
        CoTaskMemFree(pMixFmt);
        return 1;
    }

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        LOG_ERROR("IAudioClient Start failed: 0x" << std::hex << hr);
        CoTaskMemFree(pMixFmt);
        return 1;
    }

    LOG_INFO("capture started");

    std::thread writer(WriterThread);

    UINT32 packetLength = 0;
    std::vector<short> pcmData, silence;
    UINT64 totalChunks = 0, silentChunks = 0;
    UINT64 nonZeroSamples = 0;
    const UINT64 DEBUG_REPORT_INTERVAL = 100;
    // Warn once if many active chunks produce no non-zero samples (exclusive mode symptom).
    bool exclusiveWarnEmitted = false;

    while (isRunning) {
        Sleep(20);

        if (hStdin && GetFileType(hStdin) == FILE_TYPE_PIPE) {
            DWORD avail = 0;
            if (!PeekNamedPipe(hStdin, nullptr, 0, nullptr, &avail, nullptr) &&
                GetLastError() == ERROR_BROKEN_PIPE) {
                break;
            }
        }

        hr = pCaptureClient->GetNextPacketSize(&packetLength);
        if (FAILED(hr)) { LOG_ERROR("GetNextPacketSize failed: 0x" << std::hex << hr); break; }

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
                ConvertToStereoS16(pData, numFramesAvailable, pMixFmt, pcmData);
                for (short s : pcmData) if (s != 0) nonZeroSamples++;
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

        if (totalChunks > 0 && totalChunks % DEBUG_REPORT_INTERVAL == 0) {
            UINT64 active = totalChunks - silentChunks;
            LOG_DEBUG("chunks=" << totalChunks
                      << " silent=" << silentChunks
                      << " active=" << active
                      << " nonzero_samples=" << nonZeroSamples
                      << " queue=" << audioQueue.size());
            if (!exclusiveWarnEmitted && active >= 200 && nonZeroSamples == 0) {
                exclusiveWarnEmitted = true;
                LOG_WARN("receiving active buffers but all samples are zero; "
                         << "the audio device may be in exclusive mode. "
                         << "Fix: Windows Sound > Properties for \""
                         << friendlyName
                         << "\" > Advanced tab > uncheck "
                         << "\"Allow applications to take exclusive control of this device\"");
            }
        }
    }

    CoTaskMemFree(pMixFmt);
    isRunning = false;
    queueCV.notify_one();
    if (writer.joinable()) writer.join();
    pAudioClient->Stop();
    return 0;
}
