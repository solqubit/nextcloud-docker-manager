import json
from .core import ROOT, atomic


def messages(language):
    return json.loads((ROOT / 'locales' / (language + '.json')).read_text(encoding='utf-8'))


def remembered_language(state):
    """Keep the manager preference independent of restored application backups."""
    try:
        with (state.path / 'language').open(encoding='ascii') as source:
            language = source.read(16).strip()
        if language in ('es', 'en'):
            return language
    except (OSError, UnicodeError):
        pass
    # Existing installations created before the preference file keep their locale.
    return state.load()['language'] if state.configured else None


def remember_language(state, language):
    if language not in ('es', 'en'):
        raise ValueError('Unsupported language')
    atomic(state.path / 'language', language + '\n')
    if state.configured:
        cfg = state.load()
        if cfg['language'] != language:
            cfg['language'] = language
            state.activate(state.revision(cfg))
