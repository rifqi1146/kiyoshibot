import os
import logging
from PIL import ImageFont
from utils.config import FONT_DIR

log = logging.getLogger(__name__)

# Poppins statis (SIL OFL 1.1) di folder fonts/ repo — font UI dashboard /stats.
_UI_FONTS = {
    400: ("Poppins-Regular.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"),
    500: ("Poppins-Medium.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"),
    600: ("Poppins-SemiBold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"),
    700: ("Poppins-Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"),
}


def _font_bases():
    bases = []
    if FONT_DIR:
        bases.append(FONT_DIR)
    bases.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts"))
    bases.append("./fonts")
    return bases


def _find_font(name):
    for base in _font_bases():
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    return None


def get_ui_font(size, weight=400):
    """Font UI (Poppins) dengan weight 400/500/600/700.

    Fallback ke DejaVu/Liberation bila file Poppins tidak ada.
    """
    key = min(_UI_FONTS, key=lambda w: abs(w - weight))
    names = _UI_FONTS[key]
    path = _find_font(names[0])
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return get_font(list(names[1:]), size)


def get_ui_mono(size):
    """Font angka/terminal untuk dashboard (tetap mono supaya rapi sejajar)."""
    return get_font(["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "FreeMono.ttf"], size)


def get_font(font_names, size):
    """
    Robustly load a font from various locations.

    Args:
        font_names (str or list): Name(s) of the font to load (e.g. "DejaVuSans.ttf").
        size (int): Font size.

    Returns:
        ImageFont: A PIL ImageFont object.
    """
    if isinstance(font_names, str):
        font_names = [font_names]

    custom_dir = FONT_DIR

    common_paths = [
        "/usr/share/fonts/truetype/dejavu/",
        "/usr/share/fonts/truetype/liberation/",
        "/usr/share/fonts/truetype/freefont/",
        "/usr/share/fonts/",
        "C:\\Windows\\Fonts\\",
        "/Library/Fonts/",
        "/System/Library/Fonts/",
        "./",
    ]

    for name in font_names:
        if custom_dir:
            path = os.path.join(custom_dir, name)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass

        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass

        for base in common_paths:
            path = os.path.join(base, name)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass

        if os.path.isabs(name) and os.path.exists(name):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                pass

    log.warning(f"Could not find any of fonts {font_names}, falling back to default.")
    try:
        return ImageFont.load_default()
    except Exception:
        return None
