# auto-rb-recorder

Background daemon that automatically records Pioneer Rekordbox DJ sets by capturing process audio output.

- **Silence detection** — pauses recording when no audio is playing
- **Session splitting** — silence gaps create separate files
- **Auto export** — saves to `~/Music/auto-rb-recorder/` as WAV or MP3 when Rekordbox closes

---

## Installation

### Windows (Pre-built)

#### Method 1: PowerShell (Recommended)
Open PowerShell and paste this command to download and run the installer directly:

```powershell
$exe = "$env:TEMP\auto-rb-recorder-setup.exe"; Invoke-WebRequest -Uri "https://github.com/icherniukh/auto-rb-recorder/releases/latest/download/auto-rb-recorder-setup.exe" -OutFile $exe; if ($?) { Start-Process $exe -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES' -Wait }
```

The installer places the application in `%LOCALAPPDATA%\Programs\auto-rb-recorder` and the config in `%APPDATA%\rb-recorder\config.toml`. It registers a Scheduled Task so it runs automatically at login.

#### Method 2: Manual Download
1. Download the latest `auto-rb-recorder-setup.exe` from the [Releases](https://github.com/icherniukh/auto-rb-recorder/releases) page.
2. **Unblock the installer:** Right-click the downloaded `.exe` -> **Properties** -> Check **Unblock** -> **OK**.
3. Run the installer. 
   > **Note:** If you still see "Windows protected your PC", click **More info** -> **Run anyway**.

_(Optional)_ If you want to export recordings as MP3 instead of WAV, ensure `ffmpeg` is installed and available on your system `PATH`.

### macOS (Homebrew)

```bash
brew tap icherniukh/tap
brew install auto-rb-recorder
```

**Permissions (macOS 14+):**
Open Rekordbox — a system dialog will prompt for **Screen Recording** consent. If missed: **System Settings → Privacy & Security → Screen Recording → enable `auto-rb-recorder`**.

### Installation from Source

If you want to install from source instead of using pre-built binaries, you will need the appropriate build tools (Visual Studio C++ on Windows, Xcode on macOS) and Python/uv.

```bash
git clone https://github.com/icherniukh/auto-rb-recorder.git
cd auto-rb-recorder
uv pip install -e .
```
*(Note: For this to work fully, you must also compile the native capture helpers for your platform. See the Development section below).*

---

## Usage

### macOS Service
```bash
auto-rb-recorder                       # foreground
brew services start auto-rb-recorder   # background (starts at login)
```

### Windows Service
The Windows installer automatically registers a Scheduled Task to run the application in the background at login. You can also run it manually from the command line:
```bash
auto-rb-recorder        # normal execution
auto-rb-recorder -v     # verbose debug logging
```

---

## Configuration

The application uses a configuration file to customize its behavior (such as output directory, silence thresholds, and export format).

**Config file locations:**
- **Windows:** `%APPDATA%\rb-recorder\config.toml` (created automatically by the installer)
- **macOS:** `~/Library/Application Support/rb-recorder/config.toml` (you must create this manually)

You can find a complete list of settings and their default values in the [`config.default.toml`](config.default.toml) file in this repository.

---

## Development (For Contributors)

### Requirements

- **Windows:** 10 Build 19041+ (WASAPI loopback), Visual Studio 2019+ (C++ workload)
- **macOS:** 12+ (Screen Recording permission required), Xcode + Swift toolchain
- Python 3.11+
- `uv` (recommended) or `pip`

### Building Standalone Executables & Installers

The following scripts handle the entire build process: compiling the native capture helper, bundling the Python daemon using PyInstaller, and (on Windows) creating the Inno Setup installer.

**Windows**
```powershell
uv pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```
*Outputs to `dist\auto-rb-recorder.exe` and `dist\auto-rb-recorder-setup.exe`.*

**macOS**
```bash
uv pip install pyinstaller
bash scripts/build.sh --full
```
*Outputs to `dist/auto-rb-recorder`.*

### Testing

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