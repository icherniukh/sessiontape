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

# NSTextAlignment constants (avoid magic numbers inline)
_ALIGN_RIGHT = 1
_ALIGN_LEFT = 0

# NSAlert button-return constants
_ALERT_OK = 1           # NSModalResponseOK
_ALERT_FIRST = 1000     # NSAlertFirstButtonReturn


def _collapse_home(path: str) -> str:
    home = os.path.expanduser("~")
    return ("~" + path[len(home):]) if path.startswith(home) else path


def _expand_path(display: str) -> str:
    return os.path.expanduser(display)


def _alert_modal(message: str, info: str, *buttons: str) -> int:
    """Show a synchronous NSAlert and return the button index result."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(message)
    alert.setInformativeText_(info)
    for btn in buttons:
        alert.addButtonWithTitle_(btn)
    return alert.runModal()


def _label(text: str, x: float, y: float, w: float, h: float = 20) -> NSTextField:
    tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    tf.setStringValue_(text)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setAlignment_(_ALIGN_RIGHT)
    return tf


def _editable_field(x: float, y: float, w: float, h: float = 22) -> NSTextField:
    tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    tf.setBezeled_(True)
    tf.setEditable_(True)
    return tf


def _button(title: str, x: float, y: float, w: float, h: float = 28) -> NSButton:
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    btn.setTitle_(title)
    btn.setBezelStyle_(NSBezelStyleRounded)
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

    @objc.python_method
    def showWindow(self) -> None:
        if self._panel is None:
            self._build_panel()
        NSApp.activateIgnoringOtherApps_(True)
        self._panel.makeKeyAndOrderFront_(None)

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
        y = _H - _MARGIN - _ROW_H

        def row(label_text, control, y_pos):
            content.addSubview_(_label(label_text, _MARGIN, y_pos, _LABEL_W))
            content.addSubview_(control)

        # Output Folder
        dir_field = _editable_field(_CTRL_X, y, _CTRL_W - 68)
        dir_field.setEditable_(False)
        choose_btn = _button("Choose\u2026", _CTRL_X + _CTRL_W - 62, y - 2, 60)
        choose_btn.setTarget_(self)
        choose_btn.setAction_(objc.selector(self._choose_folder_clicked_, signature=b"v@:@"))
        row("Output Folder", dir_field, y)
        content.addSubview_(choose_btn)
        self._controls["output_dir"] = dir_field
        y -= _ROW_H + _ROW_GAP

        # Export Format
        fmt_popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(_CTRL_X, y, 120, _ROW_H))
        fmt_popup.addItemWithTitle_("WAV")
        fmt_popup.addItemWithTitle_("MP3")
        row("Export Format", fmt_popup, y)
        self._controls["export_format"] = fmt_popup
        y -= _ROW_H + _ROW_GAP

        # Sample Rate
        sr_popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(_CTRL_X, y, 120, _ROW_H))
        for rate in ("44100", "48000", "96000"):
            sr_popup.addItemWithTitle_(rate)
        row("Sample Rate", sr_popup, y)
        self._controls["sample_rate"] = sr_popup
        y -= _ROW_H + _ROW_GAP

        # Silence Threshold
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

        def stepper_row(label_text, key, min_val, max_val, step, suffix, y_pos):
            field = _editable_field(_CTRL_X, y_pos, 60)
            stepper = NSStepper.alloc().initWithFrame_(NSMakeRect(_CTRL_X + 64, y_pos, 18, _ROW_H))
            stepper.setMinValue_(min_val)
            stepper.setMaxValue_(max_val)
            stepper.setIncrement_(step)
            stepper.setValueWraps_(False)
            stepper.setTarget_(self)
            stepper.setAction_(objc.selector(self._stepper_changed_, signature=b"v@:@"))
            objc.setAssociated(stepper, "field_ref", field)
            objc.setAssociated(stepper, "suffix", suffix)
            unit_lbl = _label(suffix, _CTRL_X + 86, y_pos, 40, 20)
            unit_lbl.setAlignment_(_ALIGN_LEFT)
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

        # Process Name
        proc_field = _editable_field(_CTRL_X, y, _CTRL_W)
        proc_field.setPlaceholderString_("rekordbox")
        row("Process Name", proc_field, y)
        self._controls["process_name"] = proc_field

        # Cancel / Save buttons
        save_btn = _button("Save", _W - _MARGIN - 80, _MARGIN, 76)
        save_btn.setKeyEquivalent_("\r")
        save_btn.setTarget_(self)
        save_btn.setAction_(objc.selector(self._save_clicked_, signature=b"v@:@"))

        cancel_btn = _button("Cancel", _W - _MARGIN - 168, _MARGIN, 80)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_(objc.selector(self._cancel_clicked_, signature=b"v@:@"))

        content.addSubview_(save_btn)
        content.addSubview_(cancel_btn)

        self._populate_fields()

    @objc.python_method
    def _populate_fields(self) -> None:
        cfg = self._config

        self._controls["output_dir"].setStringValue_(_collapse_home(cfg.output_dir))

        self._controls["export_format"].selectItemWithTitle_(cfg.export_format.upper())
        self._controls["sample_rate"].selectItemWithTitle_(str(cfg.sample_rate))

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

    def _slider_changed_(self, sender) -> None:
        lbl = objc.getAssociated(sender, "label_ref")
        if lbl:
            lbl.setStringValue_(f"{sender.floatValue():.0f} dB")

    def _stepper_changed_(self, sender) -> None:
        field = objc.getAssociated(sender, "field_ref")
        suffix = objc.getAssociated(sender, "suffix") or ""
        if field:
            val = sender.doubleValue()
            display = f"{val:.0f}{suffix}" if val == int(val) else f"{val}{suffix}"
            field.setStringValue_(display)

    def _choose_folder_clicked_(self, _sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_("Choose Output Folder")
        if panel.runModal() == _ALERT_OK:
            url = panel.URL()
            if url:
                self._controls["output_dir"].setStringValue_(_collapse_home(url.path()))

    def _save_clicked_(self, _sender) -> None:
        cfg = self._collect_fields()
        if cfg is None:
            return

        expanded = _expand_path(cfg.output_dir)
        if not os.path.exists(expanded):
            result = _alert_modal(
                "Folder does not exist",
                f'"{expanded}" does not exist. Create it now?',
                "Create", "Cancel",
            )
            if result == _ALERT_FIRST:
                try:
                    os.makedirs(expanded, exist_ok=True)
                except OSError as exc:
                    _alert_modal("Cannot Create Folder", str(exc), "OK")
                    return
            else:
                return

        if cfg.export_format.lower() == "mp3":
            ffmpeg_found = bool(shutil.which("ffmpeg")) or any(
                os.path.exists(p)
                for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
            )
            if not ffmpeg_found:
                _alert_modal(
                    "FFmpeg not found",
                    "FFmpeg is required for MP3 encoding but was not found. "
                    "Install it with: brew install ffmpeg",
                    "OK",
                )

        config_path = platform_config_path()
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            f.write(cfg.to_toml_string())

        self._save_callback(cfg)
        self._panel.orderOut_(None)

    def _cancel_clicked_(self, _sender) -> None:
        self._populate_fields()
        self._panel.orderOut_(None)

    @objc.python_method
    def _collect_fields(self) -> Config | None:
        cfg = Config()

        cfg.output_dir = _expand_path(self._controls["output_dir"].stringValue())

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
            _alert_modal("Validation Error", "Process name must be non-empty and contain no spaces.", "OK")
            return None
        cfg.process_name = proc

        return cfg
