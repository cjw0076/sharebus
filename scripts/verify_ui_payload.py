"""Allow only the built UI and explicitly reviewed redistributable assets."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "favicon.svg": "2561bb63635c886a5cff81eb0576c46f8ce6dbce6ce7b13d0ef1f01f833c65d2",
    "assets/fonts/PretendardVariable.woff2": "9599f12fd42fc0bce1cd50b47a0c022e108d7aa64dd0d1bb0ed44f3282d900b4",
    "assets/fonts/Pretendard-LICENSE.txt": "8a2aba6114145294edbbce5998eab74320e7e07ce39999cc9f95ae2967d2eb9e",
}


def verify(directory):
    files = [path for path in directory.rglob('*') if path.is_file()]
    if not (directory / 'index.html').is_file():
        raise ValueError('Built UI index.html is missing')
    if any(not (directory / name).is_file() for name in ASSETS):
        raise ValueError('A reviewed asset or its retained license is missing')
    for path in files:
        name = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise ValueError('Linked UI assets are not allowed')
        if name in ASSETS:
            if hashlib.sha256(path.read_bytes()).hexdigest() != ASSETS[name]:
                raise ValueError('A reviewed public asset changed: ' + name)
        elif name != 'index.html' and not re.fullmatch(r'assets/[A-Za-z0-9_-]+\.(?:js|css)', name):
            raise ValueError('Unexpected public asset: ' + name)
    return len(files)


if __name__ == '__main__':
    try:
        print('Reviewed UI payload files:', verify(ROOT / 'outputs/avalokita/app/dist'))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
