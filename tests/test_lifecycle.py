"""Calendar, fresh-install policy and time-based end-of-life regression tests."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ndm.catalog import Catalog
from ndm.core import Error, State, atomic
from ndm.lifecycle import SCHEDULE_RAW, SCHEDULE_URL, parse_schedule, support_status
from ndm.operations import Manager
from ndm.i18n import messages
from ndm.terminal_ui import Terminal
from ndm.views import release_rows, support_rows
from test_foundation import configuration


SCHEDULE = '''
## Currently supported versions
| Version code | Version name | Release date | End of life | Current version | Next version
|:---|:---|:---|---:|---:|---:
| **35** | Summer | 2026-09-16 | 2027-09 | **35.0.2** (2026-10-15) | 35.0.3 (2026-11-12)
| **34** | Spring | 2026-06-09 | 2027-06 | **34.0.5** (2026-10-15) | 34.0.6 (2026-11-12)
| **33** | Winter | 2026-02-18 | 2027-02 | **33.0.10** (2026-10-15) | 33.0.11 (2026-11-12)
```mermaid
  35 : Summer : 16 Sep 2026 : 15 Sep 2027 : 35.0.2 : 15 October 2026
  34 : Spring : 09 Jun 2026 : 08 Jun 2027 : 34.0.5 : 15 October 2026
  33 : Winter : 18 Feb 2026 : 17 Feb 2027 : 33.0.10 : 15 October 2026
```
## Nextcloud 36 (DRAFT)
| Date | Info |
| 2027-02-17 | Final |
## EOL Versions
| Version code | Version name | Release date | End of life | Last version
|:---|:---|:---|---:|---:
| **32** | Autumn | 2025-09-27 | 2026-09 | 32.0.15 (2026-09-10)
'''


def policy():
    return dict(source=SCHEDULE_URL, checked='2026-10-27T00:00:00+00:00',
                branches=parse_schedule(SCHEDULE), stale=False)


def images_feed(*versions):
    return {str(n): dict(version=version, variants={'apache': {}}) for n, version in enumerate(versions)}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.catalog = Catalog(Path(self.work.name) / 'cache')
        clock = patch('ndm.lifecycle.today', return_value=dt.date(2026, 10, 27))
        clock.start()
        self.addCleanup(clock.stop)

    def feed(self, document=SCHEDULE, versions=('35.0.2', '34.0.5', '33.0.10', '32.0.15')):
        def fetch(url, **kwargs):
            return document.encode() if url == SCHEDULE_RAW else json.dumps(images_feed(*versions)).encode()
        return patch('ndm.catalog.fetch', side_effect=fetch)

    def test_exact_timeline_date_and_end_of_life_rows_are_parsed(self):
        rows = parse_schedule(SCHEDULE)
        self.assertEqual(rows['35']['support_until'], '2027-09-15')
        self.assertEqual(rows['35']['precision'], 'day')
        self.assertTrue(rows['32']['upstream_eol'])
        self.assertNotIn('36', rows)

    def test_calendar_format_failure_does_not_make_everything_supported(self):
        for document in ('unavailable', SCHEDULE.replace('2027-09', 'bad-date'), SCHEDULE.replace('**35.0.2**', '**36.0.2**')):
            with self.subTest(document=document[:20]), self.assertRaises(Error):
                parse_schedule(document)

    def test_install_lists_only_latest_and_previous_maintained_majors(self):
        with self.feed():
            options = self.catalog.installation_options()
        self.assertEqual([row['version'] for row in options], ['35.0.2', '34.0.5'])
        self.assertEqual([row['recommendation'] for row in options], ['latest', 'previous'])

    def test_new_major_appears_without_changing_the_manager(self):
        document = SCHEDULE.replace('| **35**', '| **36** | New | 2026-10-01 | 2027-10 | **36.0.1** | future\n| **35**', 1)
        with self.feed(document, ('36.0.1', '35.0.2', '34.0.5', '32.0.15')):
            self.assertEqual([row['version'] for row in self.catalog.installation_options()], ['36.0.1', '35.0.2'])

    def test_prereleases_and_unpublished_future_majors_are_not_offered(self):
        with self.feed(versions=('36.0.0rc1', '36.0.0', '35.0.2', '34.0.5')):
            self.assertEqual([row['version'] for row in self.catalog.installation_options()], ['35.0.2', '34.0.5'])

    def test_install_does_not_fall_back_to_an_eol_image(self):
        with self.feed(versions=('32.0.15',)), self.assertRaises(Error):
            self.catalog.installation_options()

    def test_older_maintained_branch_and_old_patch_are_not_obsolete(self):
        for version in ('34.0.5', '33.0.9', '35.0.0'):
            result = support_status(version, policy(), on=dt.date(2026, 10, 27))
            self.assertEqual(result['state'], 'supported')
            self.assertFalse(result['obsolete'])

    def test_calendar_dates_expire_even_when_upstream_has_not_moved_the_row(self):
        result = support_status('35.0.2', policy(), on=dt.date(2027, 9, 16))
        self.assertEqual(result['state'], 'eol')
        self.assertTrue(result['obsolete'])
        self.assertEqual(result['days_remaining'], -1)

    def test_approaching_eol_warns_before_it_expires(self):
        result = support_status('33.0.10', policy(), on=dt.date(2027, 1, 1))
        self.assertEqual(result['state'], 'ending')
        self.assertFalse(result['obsolete'])

    def test_explicit_eol_and_older_unlisted_installations_are_obsolete(self):
        for version in ('32.0.15', '9.0.0'):
            self.assertTrue(support_status(version, policy(), on=dt.date(2026, 10, 27))['obsolete'])
        self.assertEqual(support_status('36.0.0', policy())['state'], 'unknown')

    def test_last_supported_day_is_inclusive(self):
        self.assertEqual(support_status('35.0.2', policy(), on=dt.date(2027, 9, 15))['state'], 'ending')

    def test_month_precision_is_kept_if_the_exact_day_is_unavailable(self):
        rows = parse_schedule(SCHEDULE.replace('15 Sep 2027', 'TBA'))
        self.assertEqual(rows['35']['precision'], 'month')
        self.assertEqual(rows['35']['support_until'], '2027-09-30')

    def test_offline_without_cache_is_unknown_and_blocks_new_installs(self):
        with patch('ndm.catalog.fetch', side_effect=Error('offline')):
            result = self.catalog.support_status('35.0.2')
            self.assertEqual(result['state'], 'unknown')
            self.assertFalse(result['obsolete'])
            with self.assertRaises(Error):
                self.catalog.installation_options()

    def test_stale_offline_calendar_still_flags_expired_installations(self):
        cached = policy()
        path = self.catalog.cache / 'nextcloud-support.json'
        atomic(path, cached)
        os.utime(path, (0, 0))
        with patch('ndm.catalog.fetch', side_effect=Error('offline')), patch('ndm.lifecycle.today', return_value=dt.date(2027, 10, 1)):
            result = self.catalog.support_status('35.0.2')
        self.assertTrue(result['stale'])
        self.assertTrue(result['obsolete'])

    def test_fresh_cache_does_not_make_a_network_request_on_every_menu(self):
        atomic(self.catalog.cache / 'nextcloud-support.json', policy())
        with patch('ndm.catalog.fetch', side_effect=AssertionError('unexpected network')):
            self.assertEqual(self.catalog.support_status('35.0.2')['state'], 'supported')

    def test_fresh_install_refreshes_the_calendar_even_with_a_recent_cache(self):
        atomic(self.catalog.cache / 'nextcloud-support.json', policy())
        with patch('ndm.catalog.fetch', side_effect=Error('offline')):
            with self.assertRaises(Error):
                self.catalog.installation_options()

    def test_invalid_cache_fails_safely(self):
        atomic(self.catalog.cache / 'nextcloud-support.json', {'source': SCHEDULE_URL, 'checked': 'now', 'branches': {'35': {}}})
        with patch('ndm.catalog.fetch', side_effect=Error('offline')):
            self.assertEqual(self.catalog.support_status('35.0.2')['state'], 'unknown')

    def test_direct_eol_install_is_rejected_before_pulling_or_creating_resources(self):
        state = State(Path(self.work.name) / 'state').open()
        self.addCleanup(state.lock_file.close)
        manager = Manager(state)
        with self.feed(), patch.object(manager.catalog, 'resolve') as resolve:
            with self.assertRaises(Error):
                manager.install('32.0.15', supplied='INSTALL')
        resolve.assert_not_called()
        self.assertFalse(state.configured)
        self.assertIsNone(state.pending)

    def test_status_flags_obsolete_without_stopping_a_healthy_application(self):
        state = State(Path(self.work.name) / 'state').open()
        self.addCleanup(state.lock_file.close)
        cfg = configuration()
        cfg['version'] = '32.0.15'
        cfg['images']['app'] = 'nextcloud:32.0.15-apache@sha256:' + 'a' * 64
        state.activate(state.revision(cfg))
        engine = Mock(cfg=cfg)
        engine.dc.return_value = SimpleNamespace(stdout='[]')
        engine.status.return_value = {'installed': True, 'maintenance': False}
        with self.feed(), patch('ndm.operations.Engine', return_value=engine):
            result = Manager(state).status()
        self.assertTrue(result['support']['obsolete'])
        self.assertTrue(result['status']['installed'])
        engine.stop.assert_not_called()
        engine.up.assert_not_called()

    def test_daily_maintenance_reports_eol_even_when_nextcloud_is_stopped(self):
        state = State(Path(self.work.name) / 'state').open()
        self.addCleanup(state.lock_file.close)
        cfg = configuration()
        cfg['version'] = '32.0.15'
        cfg['images']['app'] = 'nextcloud:32.0.15-apache@sha256:' + 'a' * 64
        state.activate(state.revision(cfg))
        engine = Mock(cfg=cfg)
        engine.status.return_value = {'installed': False}
        with self.feed(), patch('ndm.maintenance.Engine', return_value=engine):
            result = Manager(state).maintenance()
        self.assertTrue(result['support']['obsolete'])
        self.assertTrue(any('end of life' in error for error in result['errors']))
        self.assertTrue((state.path / 'last-maintenance.json').exists())
        engine.up.assert_not_called()
        engine.stop.assert_not_called()

    def test_support_and_recommendations_have_bilingual_labels(self):
        with self.feed():
            options = self.catalog.installation_options()
        for language, recommended, obsolete in (('es', 'Recomendada', 'OBSOLETA'), ('en', 'Recommended', 'OBSOLETE')):
            ui = Terminal(messages(language))
            self.assertIn(recommended, release_rows(ui, options)[0][1])
            rows = support_rows(ui, support_status('32.0.15', policy()))
            self.assertTrue(any(obsolete in str(label) for label, _ in rows))
