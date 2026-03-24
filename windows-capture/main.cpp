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

using namespace Microsoft::WRL;

#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

// COM Smart pointers and completion handler
class CActivateAudioInterfaceCompletionHandler :
    public RuntimeClass<RuntimeClassFlags<ClassicCom>, IActivateAudioInterfaceCompletionHandler, FtmBase>
{
public:
    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* operation)
    {
        HRESULT hrActivate = S_OK;
        IUnknown* punkAudioClient = nullptr;

        HRESULT hr = operation->GetActivateResult(&hrActivate, &punkAudioClient);
        if (SUCCEEDED(hr) && SUCCEEDED(hrActivate) && punkAudioClient)
        {
            punkAudioClient->QueryInterface(IID_PPV_ARGS(&m_AudioClient));
        }
        
        SetEvent(m_hActivateCompleted);
        return S_OK;
    }

    ComPtr<IAudioClient> m_AudioClient;
    HANDLE m_hActivateCompleted = CreateEvent(nullptr, FALSE, FALSE, nullptr);
};

void PrintHelp() {
    std::cerr << "Usage: rb-capture-win.exe --pid <PID> [--sample-rate <RATE>]\n";
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

    // Set stdout to binary mode
    _setmode(_fileno(stdout), _O_BINARY);

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        std::cerr << "CoInitializeEx failed: " << std::hex << hr << "\n";
        return 1;
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

    WaitForSingleObject(handler->m_hActivateCompleted, INFINITE);

    if (!handler->m_AudioClient) {
        std::cerr << "Failed to acquire IAudioClient\n";
        return 1;
    }

    ComPtr<IAudioClient> pAudioClient = handler->m_AudioClient;

    // Process loopback virtual devices don't support GetMixFormat.
    // Build a float32 stereo format at the requested sample rate directly.
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

    bool isFloat = true;

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

    UINT32 packetLength = 0;
    while (true) {
        // Sleep for roughly half the buffer duration to avoid busy waiting
        Sleep(20); 

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
                std::vector<short> silence(numFramesAvailable * nChannels, 0);
                fwrite(silence.data(), sizeof(short), silence.size(), stdout);
            } else {
                // Format is always float32 (we set it above)
                float* pFloatData = (float*)pData;
                std::vector<short> pcmData(numFramesAvailable * nChannels);
                for (UINT32 i = 0; i < numFramesAvailable * nChannels; ++i) {
                    float sample = pFloatData[i];
                    if (sample > 1.0f) sample = 1.0f;
                    if (sample < -1.0f) sample = -1.0f;
                    pcmData[i] = (short)(sample * 32767.0f);
                }
                fwrite(pcmData.data(), sizeof(short), pcmData.size(), stdout);
            }

            fflush(stdout);

            hr = pCaptureClient->ReleaseBuffer(numFramesAvailable);
            if (FAILED(hr)) break;

            hr = pCaptureClient->GetNextPacketSize(&packetLength);
            if (FAILED(hr)) break;
        }
    }

    pAudioClient->Stop();
    CoUninitialize();

    return 0;
}
