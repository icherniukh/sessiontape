"""Preferences window for auto-rb-recorder (macOS, PyObjC)."""

import os
import shutil
from typing import Callable

import objc
from AppKit import (
    NSAlert,
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSObject,
    NSOpenPanel,
    NSPanel,
    NSPopUpButton,
    NSSlider,
    NSStepper,
    NSTextField,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakePoint, NSMakeSize

from src.config import Config, platform_config_path

# Window dimensions
_W = 480
_H = 480
_MARGIN = 24
_ROW_H = 28
_ROW_GAP = 10
_LABEL_W = 150
_CTRL_X = _MARGIN + _LABEL_W + 8
_CTRL_W = _W - _CTRL_X - _MARGIN


def _label(text: str, x: float, y: float, w: float, h: float = 20) -> NSTextField:
    f = NSMakeRect(x, y, w, h)
    tf = NSTextField.alloc().initWithFrame_(f)
    tf.setStringValue_(text)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setAlignment_(1)  # NSTextAlignmentRight
    return tf


def _editable_field(x: float, y: float, w: float, h: float = 22) -> NSTextField:
    f = NSMakeRect(x, y, w, h)
    tf = NSTextField.alloc().initWithFrame_(f)
    tf.setBezeled_(True)
    tf.setEditable_(True)
    return tf


def _button(title: str, x: float, y: float, w: float, h: float = 28) -> NSButton:
    f = NSMakeRect(x, y, w, h)
    btn = NSButton.alloc().initWithFrame_(f)
    btn.setTitle_(title)
    btn.setBezelStyle_(NSBezelStyleRounded)
    btn.setButtonType_(0)  # NSMomentaryLightButton
    return btn


class PreferencesWindowController(NSObject):

    @objc.python_method
    def initWithConfig_callback_(
        self, config: Config, save_callback: Callable[[Config], None]
    ) -> "PreferencesWindowController":
        self = objc.super(PreferencesWindowController, self).init()
        if self is None:
            return None
        self._config = config
        self._save_callback = save_callback
        self._panel = None
        self._controls: dict = {}
        return self

    # ── Public interface ──────────────────────────────────────────────────────

    @objc.python_method
    def showWindow(self) -> None:
        if self._panel is None:
            self._build_panel()
        NSApp.activateIgnoringOtherApps_(True)
        self._panel.makeKeyAndOrderFront_(None)

    # ── Panel construction ────────────────────────────────────────────────────

    @objc.python_method
    def _build_panel(self) -> None:
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _W, _H), style, NSBackingStoreBuffered, False
        )
        self._panel.setTitle_("auto-rb-recorder Preferences")
        self._panel.setLevel_(NSFloatingWindowLevel)
        self._panel.center()

        content = self._panel.contentView()
        y = _H - _MARGIN - _ROW_H  # top → bottom layout

        # Helper to add one settings row
        def row(label_text, control, y_pos):
            lbl = _label(label_text, _MARGIN, y_pos, _LABEL_W)
            content.addSubview_(lbl)
            content.addSubview_(control)

        # ── Output Folder ──────────────────────────────────────────────────
        dir_field = _editable_field(_CTRL_X, y, _CTRL_W - 68)
        dir_field.setEditable_(False)
        choose_btn = _button("Choose\u2026", _CTRL_X + _CTRL_W - 62, y - 2, 60)
        choose_btn.setTarget_(self)
        choose_btn.setAction_(objc.selector(self._choose_folder_clicked_, signature=b"v@:@"))
        row("Output Folder", dir_field, y)
        content.addSubview_(choose_btn)
        self._controls["output_dir"] = dir_field
        y -= _ROW_H + _ROW_GAP

        # ── Export Format ──────────────────────────────────────────────────
        fmt_popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(_CTRL_X, y, 120, _ROW_H))
        fmt_popup.addItemWithTitle_("WAV")
        fmt_popup.addItemWithTitle_("MP3")
        row("Export Format", fmt_popup, y)
        self._controls["export_format"] = fmt_popup
        y -= _ROW_H + _ROW_GAP

        # ── Sample Rate ────────────────────────────────────────────────────
        sr_popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(_CTRL_X, y, 120, _ROW_H))
        for rate in ("44100", "48000", "96000"):
            sr_popup.addItemWithTitle_(rate)
        row("Sample Rate", sr_popup, y)
        self._controls["sample_rate"] = sr_popup
        y -= _ROW_H + _ROW_GAP

        # ── Silence Threshold ──────────────────────────────────────────────
        slider = NSSlider.alloc().initWithFrame_(NSMakeRect(_CTRL_X, y, _CTRL_W - 60, _ROW_H))
        slider.setMinValue_(-80)
        slider.setMaxValue_(-20)
        slider.setContinuous_(True)
        slider_label = NSTextField.alloc().initWithFrame_(NSMakeRect(_CTRL_X + _CTRL_W - 54, y, 52, 20))
        slider_label.setBezeled_(False)
        slider_label.setDrawsBackground_(False)
        slider_label.setEditable_(False)
        slider.setTarget_(self)
        slider.setAction_(objc.selector(self._slider_changed_, signature=b"v@:@"))
        objc.setAssociated(slider, "label_ref", slider_label)
        row("Silence Threshold", slider, y)
        content.addSubview_(slider_label)
        self._controls["silence_threshold_db"] = slider
        self._controls["silence_threshold_label"] = slider_label
        y -= _ROW_H + _ROW_GAP

        # ── Stepper+field rows ─────────────────────────────────────────────
        def stepper_row(label_text, key, min_val, max_val, step, suffix, y_pos):
            field = _editable_field(_CTRL_X, y_pos, 60)
            stepper = NSStepper.alloc().initWithFrame_(NSMakeRect(_CTRL_X + 64, y_pos, 18, _ROW_H))
            stepper.setMinValue_(min_val)
            stepper.setMaxValue_(max_val)
            stepper.setIncrement_(step)
            stepper.setValueWraps_(False)
            stepper.setTarget_(self)
            stepper.setAction_(
                objc.selector(self._stepper_changed_, signature=b"v@:@")
            )
            objc.setAssociated(stepper, "field_ref", field)
            objc.setAssociated(stepper, "suffix", suffix)
            unit_lbl = _label(suffix, _CTRL_X + 86, y_pos, 40, 20)
            unit_lbl.setAlignment_(0)  # NSTextAlignmentLeft
            row(label_text, field, y_pos)
            content.addSubview_(stepper)
            content.addSubview_(unit_lbl)
            self._controls[key] = (field, stepper)

        stepper_row("Min Silence Duration", "min_silence_duration", 1, 120, 1, "s", y)
        y -= _ROW_H + _ROW_GAP
        stepper_row("Min Segment Duration", "min_segment_duration", 5, 300, 5, "s", y)
        y -= _ROW_H + _ROW_GAP
        stepper_row("Decay Tail", "decay_tail", 0, 30, 1, "s", y)
        y -= _ROW_H + _ROW_GAP
        stepper_row("Poll Interval", "poll_interval", 0.5, 30, 0.5, "s", y)
        y -= _ROW_H + _ROW_GAP

        # ── Process Name ───────────────────────────────────────────────────
        proc_field = _editable_field(_CTRL_X, y, _CTRL_W)
        proc_field.setPlaceholderString_("rekordbox")
        row("Process Name", proc_field, y)
        self._controls["process_name"] = proc_field
        y -= _ROW_H + _ROW_GAP

        # ── Cancel / Save buttons ──────────────────────────────────────────
        btn_y = _MARGIN
        save_btn = _button("Save", _W - _MARGIN - 80, btn_y, 76)
        save_btn.setKeyEquivalent_("\r")
        save_btn.setTarget_(self)
        save_btn.setAction_(objc.selector(self._save_clicked_, signature=b"v@:@"))

        cancel_btn = _button("Cancel", _W - _MARGIN - 168, btn_y, 80)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_(objc.selector(self._cancel_clicked_, signature=b"v@:@"))

        content.addSubview_(save_btn)
        content.addSubview_(cancel_btn)

        self._populate_fields()

    @objc.python_method
    def _populate_fields(self) -> None:
        cfg = self._config

        self._controls["output_dir"].setStringValue_(
            cfg.output_dir.replace(os.path.expanduser("~"), "~")
        )

        fmt_popup = self._controls["export_format"]
        fmt_popup.selectItemWithTitle_(cfg.export_format.upper())

        sr_popup = self._controls["sample_rate"]
        sr_popup.selectItemWithTitle_(str(cfg.sample_rate))

        slider = self._controls["silence_threshold_db"]
        slider.setFloatValue_(cfg.silence_threshold_db)
        self._controls["silence_threshold_label"].setStringValue_(
            f"{cfg.silence_threshold_db:.0f} dB"
        )

        for key, default in [
            ("min_silence_duration", cfg.min_silence_duration),
            ("min_segment_duration", cfg.min_segment_duration),
            ("decay_tail", cfg.decay_tail),
            ("poll_interval", cfg.poll_interval),
        ]:
            field, stepper = self._controls[key]
            stepper.setDoubleValue_(default)
            field.setStringValue_(str(default))

        self._controls["process_name"].setStringValue_(cfg.process_name)

    # ── Control actions ───────────────────────────────────────────────────────

    def _slider_changed_(self, sender) -> None:
        val = sender.floatValue()
        lbl = objc.getAssociated(sender, "label_ref")
        if lbl:
            lbl.setStringValue_(f"{val:.0f} dB")

    def _stepper_changed_(self, sender) -> None:
        field = objc.getAssociated(sender, "field_ref")
        suffix = objc.getAssociated(sender, "suffix") or ""
        if field:
            val = sender.doubleValue()
            # Show integer if value has no fractional part
            display = f"{val:.0f}{suffix}" if val == int(val) else f"{val}{suffix}"
            field.setStringValue_(display)

    def _choose_folder_clicked_(self, _sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_("Choose Output Folder")
        if panel.runModal() == 1:  # NSModalResponseOK
            url = panel.URL()
            if url:
                path = url.path()
                self._controls["output_dir"].setStringValue_(
                    path.replace(os.path.expanduser("~"), "~")
                )

    def _save_clicked_(self, _sender) -> None:
        cfg = self._collect_fields()
        if cfg is None:
            return

        # Validate output_dir
        expanded = os.path.expanduser(cfg.output_dir)
        if not os.path.exists(expanded):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Folder does not exist")
            alert.setInformativeText_(
                f'"{expanded}" does not exist. Create it now?'
            )
            alert.addButtonWithTitle_("Create")
            alert.addButtonWithTitle_("Cancel")
            if alert.runModal() == 1000:  # NSAlertFirstButtonReturn
                try:
                    os.makedirs(expanded, exist_ok=True)
                except OSError as exc:
                    self._show_error(f"Could not create folder: {exc}")
                    return
            else:
                return

        # Warn if MP3 and ffmpeg missing
        if cfg.export_format.lower() == "mp3" and not shutil.which("ffmpeg"):
            for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
                if os.path.exists(path):
                    break
            else:
                alert = NSAlert.alloc().init()
                alert.setMessageText_("FFmpeg not found")
                alert.setInformativeText_(
                    "FFmpeg is required for MP3 encoding but was not found. "
                    "Install it with: brew install ffmpeg"
                )
                alert.addButtonWithTitle_("OK")
                alert.runModal()

        # Write config file
        config_path = platform_config_path()
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            f.write(cfg.to_toml_string())

        self._save_callback(cfg)
        self._panel.orderOut_(None)

    def _cancel_clicked_(self, _sender) -> None:
        self._populate_fields()
        self._panel.orderOut_(None)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @objc.python_method
    def _collect_fields(self) -> Config | None:
        cfg = Config()

        cfg.output_dir = os.path.expanduser(
            self._controls["output_dir"].stringValue()
        )

        fmt = self._controls["export_format"].titleOfSelectedItem()
        cfg.export_format = fmt.lower() if fmt else "wav"

        sr_str = self._controls["sample_rate"].titleOfSelectedItem()
        try:
            cfg.sample_rate = int(sr_str) if sr_str else 48000
        except ValueError:
            cfg.sample_rate = 48000

        cfg.silence_threshold_db = float(
            self._controls["silence_threshold_db"].floatValue()
        )

        for key in ("min_silence_duration", "min_segment_duration", "decay_tail", "poll_interval"):
            field, _stepper = self._controls[key]
            raw = field.stringValue().replace("s", "").strip()
            try:
                setattr(cfg, key, float(raw))
            except ValueError:
                pass

        proc = self._controls["process_name"].stringValue().strip()
        if not proc or " " in proc:
            self._show_error("Process name must be non-empty and contain no spaces.")
            return None
        cfg.process_name = proc

        return cfg

    @objc.python_method
    def _show_error(self, message: str) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Validation Error")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()
