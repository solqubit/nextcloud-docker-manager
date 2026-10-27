import json
from .core import ROOT


def messages(language):
    return json.loads((ROOT / 'locales' / (language + '.json')).read_text(encoding='utf-8'))
