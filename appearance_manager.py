"""Пользовательская система оформления FiercyLauncher.

Цвета и геометрия хранятся отдельно от QSS, поэтому всё можно менять из UI
без ручного редактирования файлов тем.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt5.QtGui import QColor


PRESETS: dict[str, dict[str, str]] = {
    "dark": {
        "window": "#090D18", "sidebar": "#0D1424", "panel": "#121C30",
        "panel_alt": "#19263D", "input": "#0C1527", "text": "#F4F7FF",
        "muted": "#9BAAC5", "border": "#2B3A56", "accent": "#9B5CFF",
        "accent2": "#27D7E8", "button_text": "#FFFFFF", "danger": "#FF647C",
        "success": "#45D6A0", "selection": "#633AB3",
    },
    "light": {
        "window": "#EDF1F8", "sidebar": "#E3E9F3", "panel": "#FFFFFF",
        "panel_alt": "#F5F7FC", "input": "#FFFFFF", "text": "#182338",
        "muted": "#65748E", "border": "#D0D9E8", "accent": "#7146E8",
        "accent2": "#08AFC3", "button_text": "#FFFFFF", "danger": "#D83D5B",
        "success": "#168C61", "selection": "#CFC1FA",
    },
    "neon": {
        "window": "#080B12", "sidebar": "#101424", "panel": "#171B2D",
        "panel_alt": "#222842", "input": "#0D1220", "text": "#FAF7FF",
        "muted": "#A6A3C2", "border": "#39345F", "accent": "#FF4EDB",
        "accent2": "#35E8FF", "button_text": "#FFFFFF", "danger": "#FF526F",
        "success": "#56F0B5", "selection": "#9C39B5",
    },
    "emerald": {
        "window": "#08130F", "sidebar": "#0D1D17", "panel": "#14281F",
        "panel_alt": "#1E382C", "input": "#0B1A13", "text": "#F0FFF6",
        "muted": "#9AB9A7", "border": "#2D5140", "accent": "#20C982",
        "accent2": "#A3E635", "button_text": "#052116", "danger": "#FB7185",
        "success": "#42D99A", "selection": "#166B4C",
    },
    "crimson": {
        "window": "#13090E", "sidebar": "#1D1018", "panel": "#2A1722",
        "panel_alt": "#3A202D", "input": "#1A0E15", "text": "#FFF4F6",
        "muted": "#C5A5B0", "border": "#593344", "accent": "#F04472",
        "accent2": "#FF9A55", "button_text": "#FFFFFF", "danger": "#FF526F",
        "success": "#46D39B", "selection": "#8D294B",
    },
    "ocean": {
        "window": "#07121B", "sidebar": "#0A1C2B", "panel": "#102638",
        "panel_alt": "#18364D", "input": "#091A28", "text": "#F0FAFF",
        "muted": "#98B7C9", "border": "#28516B", "accent": "#2F9BFF",
        "accent2": "#21D3C5", "button_text": "#FFFFFF", "danger": "#FF647C",
        "success": "#47D9A8", "selection": "#155F91",
    },
}

DEFAULT_GEOMETRY = {
    "window_radius": 22,
    "card_radius": 20,
    "button_radius": 13,
    "control_radius": 11,
    "density": 100,
    "sidebar_width": 236,
    "titlebar_height": 66,
    "shadow_strength": 20,
    "font_family": "Segoe UI",
    "gradients": True,
    "background_gradient": True,
    "shadows": False,
    "low_effects": True,
}

COLOR_LABELS = {
    "window": "Фон окна",
    "sidebar": "Боковая панель",
    "panel": "Карточки и панели",
    "panel_alt": "Вторичные панели",
    "input": "Поля ввода",
    "text": "Основной текст",
    "muted": "Вторичный текст",
    "border": "Границы и разделители",
    "accent": "Основной акцент",
    "accent2": "Второй акцент градиента",
    "button_text": "Текст кнопок",
    "danger": "Цвет предупреждений",
    "success": "Цвет успеха",
    "selection": "Выделение текста",
}


class AppearanceManager:
    def __init__(self, settings):
        self.settings = settings

    def load(self) -> dict[str, Any]:
        mode = str(self.settings.value("appearance/theme", "dark")).lower()
        if mode not in PRESETS:
            mode = "dark"
        palette = dict(PRESETS[mode])
        raw = self.settings.value("appearance/palette_json", "")
        if raw:
            try:
                stored = json.loads(str(raw))
                if isinstance(stored, dict):
                    for key, value in stored.items():
                        if key in palette and self._valid_color(value):
                            palette[key] = QColor(str(value)).name(QColor.HexArgb) if len(str(value)) == 9 else QColor(str(value)).name()
            except (ValueError, TypeError):
                pass
        geometry = dict(DEFAULT_GEOMETRY)
        for key, default in DEFAULT_GEOMETRY.items():
            value = self.settings.value(f"appearance/{key}", default)
            if isinstance(default, bool):
                if isinstance(value, str):
                    value = value.lower() in ("1", "true", "yes", "on")
                else:
                    value = bool(value)
            elif isinstance(default, int):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    value = default
            else:
                value = str(value)
            geometry[key] = value
        geometry["window_radius"] = max(0, min(36, geometry["window_radius"]))
        geometry["card_radius"] = max(0, min(32, geometry["card_radius"]))
        geometry["button_radius"] = max(0, min(28, geometry["button_radius"]))
        geometry["control_radius"] = max(0, min(24, geometry["control_radius"]))
        geometry["density"] = max(75, min(130, geometry["density"]))
        geometry["sidebar_width"] = max(190, min(330, geometry["sidebar_width"]))
        geometry["titlebar_height"] = max(48, min(100, geometry["titlebar_height"]))
        geometry["shadow_strength"] = max(0, min(100, geometry["shadow_strength"]))
        return {"mode": mode, "palette": palette, "geometry": geometry}

    def save(self, mode: str, palette: dict[str, str], geometry: dict[str, Any]) -> None:
        mode = mode if mode in PRESETS else "custom"
        self.settings.setValue("appearance/theme", mode)
        self.settings.setValue("appearance/palette_json", json.dumps(palette, ensure_ascii=False))
        for key, value in geometry.items():
            self.settings.setValue(f"appearance/{key}", value)
        self.settings.sync()

    @staticmethod
    def _valid_color(value: Any) -> bool:
        color = QColor(str(value))
        return color.isValid()

    def stylesheet(self, data: dict[str, Any] | None = None) -> str:
        # data позволяет показывать черновое оформление без сохранения его в QSettings.
        data = data or self.load()
        p, g = data["palette"], data["geometry"]
        scale = g["density"] / 100.0
        pad = round(12 * scale)
        font = str(g["font_family"]).replace("'", "")
        r_window, r_card, r_button, r_control = (
            g["window_radius"], g["card_radius"], g["button_radius"], g["control_radius"]
        )
        if g["gradients"]:
            button_bg = (f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                         f"stop:0 {p['accent']}, stop:1 {p['accent2']})")
            button_hover = (f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                            f"stop:0 {p['accent2']}, stop:1 {p['accent']})")
        else:
            button_bg = p["accent"]
            button_hover = p["accent2"]
        if g["background_gradient"]:
            window_bg = (f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                         f"stop:0 {p['window']}, stop:1 {p['panel_alt']})")
            hero_bg = (f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                        f"stop:0 {p['panel_alt']}, stop:0.58 {p['panel']}, stop:1 {p['sidebar']})")
        else:
            window_bg, hero_bg = p["window"], p["panel"]
        shadow = "rgba(0,0,0,105)" if g["shadows"] and not g.get("low_effects", False) else "transparent"
        return f"""
* {{ font-family: '{font}', 'Segoe UI', sans-serif; font-size: {round(10*scale)}pt; }}
QWidget {{ color: {p['text']}; background: transparent; }}
QMainWindow {{ background: transparent; }}
#WindowRoot {{ background: {window_bg}; border: 1px solid {p['border']}; border-radius: {r_window}px; }}
#TitleBar {{ background: {p['sidebar']}; border-bottom: 1px solid {p['border']}; border-top-left-radius: {r_window}px; border-top-right-radius: {r_window}px; }}
#BrandMark {{ background: {button_bg}; color: {p['button_text']}; border-radius: 12px; font-size: 17pt; font-weight: 900; }}
#WindowTitle {{ color: {p['text']}; font-size: 10pt; font-weight: 900; letter-spacing: 1.5px; }}
#WindowSubtitle {{ color: {p['muted']}; font-size: 8pt; }}
QToolButton#WindowControl {{ background: {p['panel_alt']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: {r_button}px; min-width: 46px; min-height: 38px; font-size: 14pt; }}
QToolButton#WindowControl:hover {{ background: {p['accent']}; color: {p['button_text']}; }}
QToolButton#CloseControl {{ background: transparent; color: {p['text']}; border: 1px solid transparent; border-radius: {r_button}px; min-width: 46px; min-height: 38px; font-size: 16pt; }}
QToolButton#CloseControl:hover {{ background: {p['danger']}; color: #FFFFFF; }}
#Sidebar {{ background: {p['sidebar']}; border-right: 1px solid {p['border']}; }}
#NavButton {{ text-align: left; padding: {pad+3}px {pad+5}px; border: 1px solid transparent; border-radius: {r_button}px; color: {p['muted']}; background: transparent; font-weight: 700; min-height: {round(28*scale)}px; }}
#NavButton:hover {{ background: {p['panel_alt']}; color: {p['text']}; border-color: {p['border']}; }}
#NavButton[active="true"] {{ background: {button_bg}; color: {p['button_text']}; border: 1px solid {p['accent2']}; }}
#PageHeading {{ color: {p['text']}; font-size: 24pt; font-weight: 900; }}
#Eyebrow {{ color: {p['accent2']}; font-size: 8pt; font-weight: 900; letter-spacing: 2px; }}
#Muted {{ color: {p['muted']}; }}
#Card {{ background: {p['panel']}; border: 1px solid {p['border']}; border-radius: {r_card}px; }}
#HeroCard {{ background: {hero_bg}; border: 1px solid {p['accent']}; border-radius: {r_card+4}px; }}
#HeroTitle {{ color: {p['text']}; font-size: 29pt; font-weight: 950; }}
#HeroEmblem {{ background: {button_bg}; color: {p['button_text']}; border: 2px solid {p['accent2']}; border-radius: {r_card+8}px; font-size: 58pt; font-weight: 950; }}
#HeroCopy {{ color: {p['muted']}; font-size: 10pt; }}
#PrimaryButton {{ color: {p['button_text']}; background: {button_bg}; border: 1px solid {p['accent2']}; border-radius: {r_button}px; padding: {pad+3}px {pad+12}px; font-weight: 900; min-height: {round(26*scale)}px; }}
#PrimaryButton:hover {{ background: {button_hover}; border-color: {p['text']}; }}
#PrimaryButton:pressed {{ padding-top: {pad+5}px; }}
#PrimaryButton:disabled {{ background: {p['panel_alt']}; color: {p['muted']}; border-color: {p['border']}; }}
#SecondaryButton {{ color: {p['text']}; background: {p['panel_alt']}; border: 1px solid {p['border']}; border-radius: {r_button}px; padding: {pad}px {pad+7}px; font-weight: 750; min-height: {round(25*scale)}px; }}
#SecondaryButton:hover {{ background: {p['accent']}; color: {p['button_text']}; border-color: {p['accent2']}; }}
#DangerButton {{ color: {p['danger']}; background: {p['panel_alt']}; border: 1px solid {p['danger']}; border-radius: {r_button}px; padding: {pad}px {pad+7}px; font-weight: 800; }}
#DangerButton:hover {{ color: #FFFFFF; background: {p['danger']}; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: {r_control}px; color: {p['text']}; padding: {pad-2}px {pad}px; selection-background-color: {p['selection']}; min-height: {round(24*scale)}px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 2px solid {p['accent']}; }}
QComboBox::drop-down {{ border: 0; width: 30px; }}
QComboBox QAbstractItemView {{ background: {p['panel']}; border: 1px solid {p['border']}; selection-background-color: {p['selection']}; color: {p['text']}; outline: 0; padding: 5px; }}
QCheckBox {{ spacing: 10px; color: {p['text']}; }}
QCheckBox::indicator {{ width: 19px; height: 19px; border-radius: 6px; border: 1px solid {p['border']}; background: {p['input']}; }}
QCheckBox::indicator:checked {{ background: {p['accent']}; border: 2px solid {p['accent2']}; }}
QProgressBar {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 8px; height: 15px; text-align: center; color: {p['text']}; }}
QProgressBar::chunk {{ background: {button_bg}; border-radius: 7px; }}
QPlainTextEdit {{ font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 9pt; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 3px; }}
QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSizeGrip {{ background: transparent; width: 16px; height: 16px; }}
QLabel#Pill {{ background: {p['panel_alt']}; border: 1px solid {p['success']}; border-radius: 12px; padding: 6px 10px; color: {p['success']}; font-weight: 800; }}
QLabel#AccountPill {{ background: {p['panel_alt']}; border: 1px solid {p['accent']}; border-radius: 12px; padding: 8px 12px; color: {p['text']}; font-weight: 800; }}
QLabel#StatusText {{ color: {p['muted']}; font-size: 9pt; }}
QLabel#SmallNote {{ color: {p['muted']}; font-size: 8pt; }}
QFrame#Divider {{ background: {p['border']}; max-height: 1px; }}
QToolTip {{ color: {p['text']}; background: {p['panel']}; border: 1px solid {p['accent']}; padding: 7px 10px; border-radius: 8px; }}
QLabel#SkinAvatar {{ background: {p['panel_alt']}; border: 2px solid {p['accent']}; border-radius: 20px; color: {p['accent2']}; font-size: 32pt; font-weight: 900; }}
QLabel#PromoBanner {{ background: {p['panel_alt']}; border: 1px solid {p['border']}; border-radius: {r_card}px; padding: 7px; }}
QFrame#ToastCard {{ background: {p['panel_alt']}; border: 1px solid {p['accent']}; border-radius: 16px; }}
QLabel#ToastTitle {{ color: {p['text']}; font-size: 10pt; font-weight: 900; }}
QLabel#ToastMessage {{ color: {p['muted']}; font-size: 9pt; }}
QLabel#ToastIcon {{ color: {p['accent2']}; font-size: 19pt; font-weight: 900; }}
QToolButton#ToastClose {{ color: {p['muted']}; border: 0; background: transparent; font-size: 14pt; }}
QPushButton#ColorButton {{ min-width: 100px; max-width: 100px; min-height: 30px; border-radius: 8px; border: 1px solid {p['border']}; }}
QSlider::groove:horizontal {{ height: 7px; background: {p['panel_alt']}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 18px; margin: -6px 0; border-radius: 9px; background: {p['accent']}; border: 2px solid {p['accent2']}; }}
"""
