#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
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

#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

// COM Smart pointers and completion handler
class CActivateAudioInterfaceCompletionHandler :
    public RuntimeClass<RuntimeClassFlags<ClassicCom>, IActivateAudioInterfaceCompletionHandler, FtmBase>
{
public:
    CActivateAudioInterfaceCompletionHandler() {
        m_hActivateCompleted = CreateEvent(nullptr, FALSE, FALSE, nullptr);
        if (m_hActivateCompleted == nullptr) {
            std::cerr << "CreateEvent failed: " << GetLastError() << "\n";
            m_initFailed = true;
        }
    }

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* operation)
    {
        HRESULT hrActivate = S_OK;
        IUnknown* punkAudioClient = nullptr;

        HRESULT hr = operation->GetActivateResult(&hrActivate, &punkAudioClient);
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate) && punkAudioClient)
        {
            punkAudioClient->QueryInterface(IID_PPV_ARGS(&m_AudioClient));
        }

        if (m_hActivateCompleted) SetEvent(m_hActivateCompleted);
        return S_OK;
    }

    ~CActivateAudioInterfaceCompletionHandler() {
        if (m_hActivateCompleted) CloseHandle(m_hActivateCompleted);
    }

    ComPtr<IAudioClient> m_AudioClient;
    HANDLE m_hActivateCompleted = nullptr;
    bool m_initFailed = false;
};

void PrintHelp() {
    std::cerr << "Usage: sessiontape-capture-win.exe --pid <PID> [--sample-rate <RATE>]\n";
}

// Global state for IPC writer thread
std::queue<std::vector<short>> audioQueue;
std::mutex queueMutex;
std::condition_variable queueCV;
std::atomic<bool> isRunning{true};
const size_t MAX_QUEUE_CHUNKS = 5000; // ~100 seconds of audio to prevent OOM

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
        // Return 0 means an actual I/O error or the reader closed the pipe.
        size_t written = 0;
        size_t total = chunk.size();
        bool writeError = false;
        while (written < total) {
            size_t w = fwrite(chunk.data() + written, sizeof(short), total - written, stdout);
            if (w == 0) {
                isRunning = false;
                queueCV.notify_all();
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
        isRunning = false;
        queueCV.notify_all();
        return TRUE;
    }
    return FALSE;
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
        }
    }

    if (targetPid == 0) {
        PrintHelp();
        return 1;
    }

    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    if (hStdin == INVALID_HANDLE_VALUE || hStdin == nullptr) hStdin = nullptr;

    // Set stdout to binary mode.
    // Use default _IOFBF since we explicitly fflush in the writer thread.
    _setmode(_fileno(stdout), _O_BINARY);
    char stdoutBuffer[65536];
    setvbuf(stdout, stdoutBuffer, _IOFBF, sizeof(stdoutBuffer));

    struct CoUninitGuard {
        ~CoUninitGuard() { CoUninitialize(); }
    };

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        std::cerr << "CoInitializeEx failed: " << std::hex << hr << "\n";
        return 1;
    }
    CoUninitGuard coinitGuard;

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
        std::cerr << "Failed to create activation event\n";
        return 1;
    }
    ComPtr<IActivateAudioInterfaceAsyncOperation> asyncOp;

    std::cerr << "Activating audio interface for PID " << targetPid << "...\n";

    hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, 
        __uuidof(IAudioClient), 
        &prop, 
        handler.Get(), 
        &asyncOp
    );

    if (FAILED(hr)) {
        std::cerr << "ActivateAudioInterfaceAsync failed: " << std::hex << hr << "\n";
        return 1;
    }

    if (WaitForSingleObject(handler->m_hActivateCompleted, INFINITE) != WAIT_OBJECT_0) {
        std::cerr << "WaitForSingleObject failed\n";
        return 1;
    }

    if (!handler->m_AudioClient) {
        std::cerr << "Failed to acquire IAudioClient\n";
        return 1;
    }

    ComPtr<IAudioClient> pAudioClient = handler->m_AudioClient;

    WAVEFORMATEXTENSIBLE wfex = {};
    wfex.Format.wFormatTag      = WAVE_FORMAT_EXTENSIBLE;
    wfex.Format.nChannels       = 2;
    wfex.Format.nSamplesPerSec  = targetSampleRate;
    wfex.Format.wBitsPerSample  = 32;
    wfex.Format.nBlockAlign     = wfex.Format.nChannels * (wfex.Format.wBitsPerSample / 8);
    wfex.Format.nAvgBytesPerSec = wfex.Format.nSamplesPerSec * wfex.Format.nBlockAlign;
    wfex.Format.cbSize          = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    wfex.Samples.wValidBitsPerSample = 32;
    wfex.dwChannelMask          = SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT;
    wfex.SubFormat              = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;

    hr = pAudioClient->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK,
        10000000, // 1 second buffer
        0,
        &wfex.Format,
        nullptr
    );

    if (FAILED(hr)) {
        std::cerr << "Initialize failed: " << std::hex << hr << "\n";
        return 1;
    }

    ComPtr<IAudioCaptureClient> pCaptureClient;
    hr = pAudioClient->GetService(IID_PPV_ARGS(&pCaptureClient));
    if (FAILED(hr)) {
        std::cerr << "GetService(IAudioCaptureClient) failed\n";
        return 1;
    }

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        std::cerr << "Start failed\n";
        return 1;
    }

    std::cerr << "Capture started successfully.\n";
    
    // Start dedicated writer thread
    std::thread writer(WriterThread);

    UINT32 packetLength = 0;
    std::vector<short> pcmData;
    std::vector<short> silence;

    while (isRunning) {
        Sleep(20);

        // Detect parent process closing the stdin pipe (clean shutdown protocol)
        if (hStdin && GetFileType(hStdin) == FILE_TYPE_PIPE) {
            DWORD avail = 0;
            if (!PeekNamedPipe(hStdin, nullptr, 0, nullptr, &avail, nullptr) &&
                GetLastError() == ERROR_BROKEN_PIPE) {
                break;
            }
        }

        hr = pCaptureClient->GetNextPacketSize(&packetLength);
        if (FAILED(hr)) break;

        while (packetLength != 0) {
            BYTE* pData;
            UINT32 numFramesAvailable;
            DWORD flags;

            hr = pCaptureClient->GetBuffer(
                &pData,
                &numFramesAvailable,
                &flags,
                nullptr,
                nullptr
            );

            if (FAILED(hr)) break;

            DWORD nChannels = wfex.Format.nChannels;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                silence.assign(numFramesAvailable * nChannels, 0);
                std::lock_guard<std::mutex> lock(queueMutex);
                if (audioQueue.size() >= MAX_QUEUE_CHUNKS) audioQueue.pop();
                audioQueue.push(silence);
            } else {
                float* pFloatData = (float*)pData;
                pcmData.resize(numFramesAvailable * nChannels);
                for (UINT32 i = 0; i < numFramesAvailable * nChannels; ++i) {
                    float sample = pFloatData[i];
                    if (sample > 1.0f) sample = 1.0f;
                    if (sample < -1.0f) sample = -1.0f;
                    pcmData[i] = (short)(sample * 32767.0f);
                }
                std::lock_guard<std::mutex> lock(queueMutex);
                if (audioQueue.size() >= MAX_QUEUE_CHUNKS) audioQueue.pop();
                audioQueue.push(pcmData);
            }
            
            queueCV.notify_one();

            hr = pCaptureClient->ReleaseBuffer(numFramesAvailable);
            if (FAILED(hr)) break;

            hr = pCaptureClient->GetNextPacketSize(&packetLength);
            if (FAILED(hr)) break;
        }
    }

    isRunning = false;
    queueCV.notify_one();
    if (writer.joinable()) {
        writer.join();
    }

    pAudioClient->Stop();

    return 0;
}
