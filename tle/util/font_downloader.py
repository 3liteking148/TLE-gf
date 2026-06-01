import logging
import os
import urllib.request

from tle import constants

# Fonts are fetched as raw files from upstream font repos. Cairo+Pango selects
# them by their embedded family name, not the file name, so only their presence
# in FONTS_DIR matters. The sans/CJK collections cover Latin + CJK text; the
# emoji fonts cover emoji codepoints (color preferred, monochrome outline as a
# fallback). A failed download only logs a warning rather than aborting startup:
# without these the image-rendering commands degrade, but the rest of the bot
# still runs. (The old Noto storage bucket these came from is now defunct, which
# is why fresh deploys need a live source.)
_FONTS = [
    (constants.NOTO_SANS_CJK_BOLD_FONT_PATH,
     'https://github.com/notofonts/noto-cjk/raw/main/Sans/OTC/NotoSansCJK-Bold.ttc'),
    (constants.NOTO_SANS_CJK_REGULAR_FONT_PATH,
     'https://github.com/notofonts/noto-cjk/raw/main/Sans/OTC/NotoSansCJK-Regular.ttc'),
    (constants.NOTO_COLOR_EMOJI_FONT_PATH,
     'https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf'),
    (constants.NOTO_EMOJI_FONT_PATH,
     'https://github.com/google/fonts/raw/main/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf'),
]

logger = logging.getLogger(__name__)


def _download(font_path, url):
    font = os.path.basename(font_path)
    logger.info(f'Downloading font `{font}`.')
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    # Write to a temp file and rename into place so an interrupted write can't
    # leave a truncated font that the isfile() check would treat as complete.
    tmp_path = font_path + '.part'
    try:
        with open(tmp_path, 'wb') as f:
            f.write(data)
        os.replace(tmp_path, font_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def maybe_download():
    for font_path, url in _FONTS:
        if os.path.isfile(font_path):
            continue
        try:
            _download(font_path, url)
        except Exception:
            logger.warning(f'Failed to download font `{os.path.basename(font_path)}`; '
                           'rendered images may not display correctly.', exc_info=True)
