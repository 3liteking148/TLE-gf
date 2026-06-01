import logging
import os
import urllib.request

from zipfile import ZipFile
from io import BytesIO

from tle import constants

URL_BASE = 'https://noto-website-2.storage.googleapis.com/pkgs/'

# Fonts packaged as zip archives on the legacy Noto bucket.
_ZIP_FONTS = [constants.NOTO_SANS_CJK_BOLD_FONT_PATH,
              constants.NOTO_SANS_CJK_REGULAR_FONT_PATH]

# Emoji fonts served as raw .ttf files. Cairo+Pango selects these by their
# embedded family name ('Noto Color Emoji' / 'Noto Emoji'), not the file
# name, so only their presence in FONTS_DIR matters. The color font (a CBDT
# bitmap font) is preferred; the monochrome outline font is a fallback for
# renderers that can't draw color glyphs. These are non-essential, so a
# failed download only logs a warning rather than aborting startup.
_DIRECT_FONTS = [
    (constants.NOTO_COLOR_EMOJI_FONT_PATH,
     'https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf'),
    (constants.NOTO_EMOJI_FONT_PATH,
     'https://github.com/google/fonts/raw/main/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf'),
]

logger = logging.getLogger(__name__)


def _unzip(font, archive):
    with ZipFile(archive) as zipfile:
        if font not in zipfile.namelist():
            raise KeyError(f'Expected font file {font} not present in downloaded zip archive.')
        zipfile.extract(font, constants.FONTS_DIR)


def _download_zip(font_path):
    font = os.path.basename(font_path)
    logger.info(f'Downloading font `{font}`.')
    with urllib.request.urlopen(f'{URL_BASE}{font}.zip') as resp:
        _unzip(font, BytesIO(resp.read()))


def _download_direct(font_path, url):
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
    for font_path in _ZIP_FONTS:
        if not os.path.isfile(font_path):
            _download_zip(font_path)
    for font_path, url in _DIRECT_FONTS:
        if not os.path.isfile(font_path):
            try:
                _download_direct(font_path, url)
            except Exception:
                logger.warning(f'Failed to download emoji font `{os.path.basename(font_path)}`; '
                               'emoji in rendered images may not display.', exc_info=True)
