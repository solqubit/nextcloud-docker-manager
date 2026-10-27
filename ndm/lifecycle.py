"""Nextcloud's official community release calendar and date-based support state."""
import calendar
import datetime as dt
import re

from .core import Error, version_tuple


SCHEDULE_URL = 'https://github.com/nextcloud/server/wiki/Maintenance-and-Release-Schedule'
SCHEDULE_RAW = 'https://raw.githubusercontent.com/wiki/nextcloud/server/Maintenance-and-Release-Schedule.md'
MONTHS = {name: index + 1 for index, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}


def today():
    return dt.datetime.now(dt.timezone.utc).date()


def parse_schedule(document):
    """Only published support/EOL tables count; draft release plans are ignored."""
    branches, section = {}, None
    for line in document.splitlines():
        if line.startswith('## '):
            title = line[3:].strip().casefold()
            section = 'supported' if title == 'currently supported versions' else 'eol' if title == 'eol versions' else None
        if not section or not line.startswith('|'):
            continue
        cells = [re.sub(r'[*_`]', '', cell).strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) < 5 or not re.fullmatch(r'[0-9]+', cells[0]):
            continue
        major = cells[0]
        try:
            released = dt.date.fromisoformat(cells[2])
            end = cells[3]
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}', end):
                year, month = map(int, end.split('-'))
                until = dt.date(year, month, calendar.monthrange(year, month)[1])
                precision = 'month'
            else:
                until, precision = dt.date.fromisoformat(end), 'day'
            current = re.match(r'([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)', cells[4])
            if not current or version_tuple(current[1])[0] != int(major) or until < released or major in branches:
                raise ValueError('Invalid release row')
            branches[major] = dict(released=released.isoformat(), support_until=until.isoformat(),
                                   precision=precision, upstream_eol=section == 'eol', current=current[1])
        except (ValueError, IndexError) as exc:
            raise Error('Cannot interpret official Nextcloud maintenance dates') from exc
    if not branches or not any(not row['upstream_eol'] for row in branches.values()):
        raise Error('Official Nextcloud support calendar could not be verified')
    # The table sometimes specifies only a month. Prefer its exact supported-
    # until date from the official Mermaid timeline when available.
    for line in document.splitlines():
        parts = [part.strip() for part in line.split(':')]
        if len(parts) < 4 or parts[0] not in branches:
            continue
        match = re.fullmatch(r'([0-9]{1,2}) ([A-Za-z]+) ([0-9]{4})', parts[3])
        if not match or match[2][:3].lower() not in MONTHS:
            continue
        try:
            until = dt.date(int(match[3]), MONTHS[match[2][:3].lower()], int(match[1]))
        except ValueError:
            continue
        row = branches[parts[0]]
        if row['precision'] == 'month' and until.isoformat()[:7] == row['support_until'][:7]:
            row.update(support_until=until.isoformat(), precision='day')
    return branches


def support_status(version, policy, *, on=None):
    on = on or today()
    major = str(version_tuple(version)[0])
    result = dict(version=version, major=int(major), state='unknown', obsolete=False,
                  source=SCHEDULE_URL, checked=policy.get('checked'), stale=policy.get('stale', False))
    row = policy.get('branches', {}).get(major)
    if not row:
        maintained = [int(key) for key, value in policy.get('branches', {}).items()
                      if not value['upstream_eol']]
        if maintained and int(major) < min(maintained):
            result.update(state='eol', obsolete=True)
        return result
    until = dt.date.fromisoformat(row['support_until'])
    if on < dt.date.fromisoformat(row['released']):
        return result
    remaining = (until - on).days
    expired = row['upstream_eol'] or remaining < 0
    result.update(state='eol' if expired else 'ending' if remaining <= 60 else 'supported',
                  obsolete=expired, support_until=until.isoformat(), precision=row['precision'],
                  days_remaining=remaining, latest_patch=row['current'])
    return result
