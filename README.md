# auto-rb-recorder

Background daemon that automatically records Pioneer Rekordbox DJ sets by capturing process audio output.

- **Silence detection** — pauses recording when no audio is playing
- **Session splitting** — silence gaps create separate files
- **Auto export** — saves to `~/Music/auto-rb-recorder/` as WAV or MP3 when Rekordbox closes

---

## Windows Installation

### Using PowerShell (Recommended - Bypasses SmartScreen)

The easiest way to install is via PowerShell. This method downloads and launches the installer directly, bypassing the "Windows protected your PC" SmartScreen warning that occurs with manual browser downloads.

Open PowerShell and paste this command:

```powershell
$exe = "$env:TEMP\auto-rb-recorder-setup.exe"; curl.exe -sfL "https://github.com/icherniukh/auto-rb-recorder/releases/latest/download/auto-rb-recorder-setup.exe" -o $exe; if ($?) { Start-Process $exe -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES' -Wait }
```

The installer places the application in `%LOCALAPPDATA%\Programs\auto-rb-recorder` and the config in `%APPDATA%\rb-recorder\config.toml`. It registers a Scheduled Task so it runs automatically at login.

### Manual Download

1. Download the latest `auto-rb-recorder-setup.exe` from the [Releases](https://github.com/icherniukh/auto-rb-recorder/releases) page.
2. **Unblock the installer:** Right-click the downloaded `.exe` -> **Properties** -> Check **Unblock** -> **OK**.
   - *Alternatively, run this in PowerShell:* `Unblock-File -Path "$env:USERPROFILE\Downloads\auto-rb-recorder-setup.exe"`
3. Run the installer. 
   > **Note:** If you still see "Windows protected your PC", click **More info** -> **Run anyway**. This happens because the executable is unsigned.

_(Optional)_ If you want to export recordings as MP3 instead of WAV, ensure `ffmpeg` is installed and available on your system `PATH`.

### From Source (For Developers)

#### Requirements

- **Windows:** 10 Build 19041+ (WASAPI loopback), Visual Studio 2019+ (C++ workload)
- **macOS:** 12+ (Screen Recording permission required), Xcode + Swift toolchain
- Python 3.11+
- `uv` (recommended) or `pip`

#### 1. Build and Package

The following scripts handle the entire build process: compiling the native capture helper, bundling the Python daemon, and (on Windows) creating the installer.

**Windows**
```powershell
git clone https://github.com/icherniukh/auto-rb-recorder.git
cd auto-rb-recorder
uv pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```
*Outputs to `dist\auto-rb-recorder.exe` and `dist\auto-rb-recorder-setup.exe`.*

**macOS**
```bash
git clone https://github.com/icherniukh/auto-rb-recorder.git
cd auto-rb-recorder
uv pip install pyinstaller
bash scripts/build.sh --full
```
*Outputs to `dist/auto-rb-recorder`.*

#### 2. Manual Installation (Optional)

If you don't want to use the pre-built installer, you can install the package in editable mode:

```bash
uv pip install -e .
```

#### 3. Create Config

```powershell
# Windows
New-Item -ItemType Directory -Force "$env:APPDATA\rb-recorder"
Copy-Item config.default.toml "$env:APPDATA\rb-recorder\config.toml"

# macOS
mkdir -p "~/Library/Application Support/rb-recorder"
cp config.default.toml "~/Library/Application Support/rb-recorder/config.toml"
```

Edit the `config.toml` as needed (see [Configuration](#configuration)).

#### 4. Run

```bash
auto-rb-recorder        # normal execution
auto-rb-recorder -v     # verbose debug logging
```

---

## macOS Installation (Homebrew)

### 1. Install

```bash
brew tap icherniukh/tap
brew install auto-rb-recorder
```

### 2. Permissions (macOS 14+)

Open Rekordbox — a system dialog will prompt for **Screen Recording** consent. If missed: **System Settings → Privacy & Security → Screen Recording → enable `auto-rb-recorder`**.

### 3. Run

```bash
auto-rb-recorder                       # foreground
brew services start auto-rb-recorder   # background (starts at login)
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

For quick debugging, you can override the backend via environment variable:

```bash
RB_CAPTURE_BACKEND=mac-capture auto-rb-recorder
```

---

## Testing

```bash
uv run pytest tests/ -v
```

---

## Architecture

| File | Role |
|------|------|
| `src/daemon.py` | Orchestrator — drives capture lifecycle on Rekordbox start/stop |
| `src/process_monitor.py` | Polls for Rekordbox process with debounce |
| `src/capture.py` | Selects platform backend, feeds PCM to recorder |
| `src/backends/macos_capture.py` | Spawns `mac-capture`, reads PCM from stdout |
| `src/backends/windows_capture.py` | Spawns `rb-capture-win.exe`, reads PCM from stdout |
| `src/recorder_core.py` | Silence detection, raw session writing, WAV/MP3 export |
| `windows-capture/main.cpp` | Native WASAPI process loopback helper (Windows) |
| `mac-capture/` | Native CoreAudio process tap helper (macOS) |
