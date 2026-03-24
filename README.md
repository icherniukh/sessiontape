# auto-rb-recorder

Background daemon that automatically records Pioneer Rekordbox DJ sets by capturing process audio output.

- **Silence detection** — pauses recording when no audio is playing
- **Session splitting** — silence gaps create separate files
- **Auto export** — saves to `~/Music/auto-rb-recorder/` as WAV or MP3 when Rekordbox closes

---

## Windows Installation

### Requirements

- Windows 10 Build 19041+ (WASAPI process loopback)
- Python 3.11+
- Visual Studio 2019+ with **Desktop development with C++** workload
- _(optional)_ `ffmpeg` on PATH for MP3 export

### 1. Clone

```powershell
git clone https://github.com/icherniukh/auto-rb-recorder.git
cd auto-rb-recorder
```

### 2. Build the native capture helper

```powershell
powershell -ExecutionPolicy Bypass -File windows-capture\build.ps1
```

Produces `windows-capture\rb-capture-win.exe`. The script will locate MSVC automatically via `vswhere` if `cl.exe` is not already on PATH.

### 3. Install the Python package

```powershell
pip install -e .
```

### 4. Place the capture helper on PATH

```powershell
$scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
Copy-Item windows-capture\rb-capture-win.exe $scripts
```

### 5. Create config

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\rb-recorder"
Copy-Item config.default.toml "$env:APPDATA\rb-recorder\config.toml"
```

Edit `%APPDATA%\rb-recorder\config.toml` as needed (see [Configuration](#configuration)).

### 6. Run

```powershell
auto-rb-recorder        # foreground
auto-rb-recorder -v     # verbose
```

### 7. Auto-start at login (optional)

Requires a standalone exe built with PyInstaller:

```powershell
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

To uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
```

---

## macOS Installation

### Requirements

- macOS 12+
- Homebrew

### 1. Install

```bash
brew tap icherniukh/tap
brew install auto-rb-recorder
```

### 2. Grant Screen Recording permission (macOS 14+)

Open Rekordbox once — a system dialog will prompt for **Screen Recording** consent. If missed: **System Settings → Privacy & Security → Screen Recording → enable `auto-rb-recorder`**.

### 3. Run

```bash
auto-rb-recorder                       # foreground
brew services start auto-rb-recorder   # background, starts at login
```

---

## Configuration

Config file locations:
- **Windows:** `%APPDATA%\rb-recorder\config.toml`
- **macOS:** `~/Library/Application Support/rb-recorder/config.toml`

```toml
[recording]
sample_rate = 48000                      # must match system audio output rate
output_dir = "~/Music/auto-rb-recorder"
export_format = "wav"                    # "wav" or "mp3" (mp3 requires ffmpeg)

[trigger]
silence_threshold_db = -50              # dB level below which audio counts as silence
min_silence_duration = 15               # seconds of silence before closing a session
decay_tail = 5                          # seconds of pre-sound buffer kept at session start

[monitor]
process_name = "rekordbox"
poll_interval = 2.0
```

---

## Building a standalone executable from source

**macOS**
```bash
git clone --recurse-submodules https://github.com/icherniukh/auto-rb-recorder.git
cd auto-rb-recorder
pip install pyinstaller
bash scripts/build.sh
# → dist/auto-rb-recorder
```

**Windows**
```powershell
git clone https://github.com/icherniukh/auto-rb-recorder.git
cd auto-rb-recorder
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
# → dist\auto-rb-recorder.exe
```

---

## Testing

```bash
pip install pytest
pytest tests/ -v
```

---

## Architecture

| File | Role |
|------|------|
| `src/daemon.py` | Orchestrator — drives capture lifecycle on Rekordbox start/stop |
| `src/process_monitor.py` | Polls for Rekordbox process with debounce |
| `src/capture.py` | Selects platform backend, feeds PCM to recorder |
| `src/backends/windows_capture.py` | Spawns `rb-capture-win.exe`, reads PCM from stdout |
| `src/recorder_core.py` | Silence detection, raw session writing, WAV/MP3 export |
| `windows-capture/main.cpp` | Native WASAPI process loopback helper (Windows) |
