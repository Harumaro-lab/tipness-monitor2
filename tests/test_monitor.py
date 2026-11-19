import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

for key in ('TIPNESS_EMAIL', 'TIPNESS_PASSWORD', 'NTFY_TOPIC'):
    os.environ.setdefault(key, 'test-only')
spec = importlib.util.spec_from_file_location('monitor', Path(__file__).parents[1] / 'monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def response(code=200, body='OK', headers=None):
    r = m.requests.Response()
    r.status_code, r._content, r.encoding = code, body.encode(), 'utf-8'
    r.headers.update(headers or {})
    return r


class FixedNow(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 19, 21, 0, tzinfo=m.JST)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, filename in [('STATE_FILE', 'state.json'), ('STATUS_FILE', 'status.json')]:
            p = patch.object(m, name, Path(self.temp.name) / filename)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(m.datetime, 'datetime', FixedNow)
        p.start()
        self.addCleanup(p.stop)
        m.OBSERVED_MONTHS.clear()

    def test_get_recovers_after_timeout_and_503(self):
        session = Mock()
        session.request.side_effect = [m.requests.exceptions.Timeout(), response(503), response()]
        with patch.object(m.time, 'sleep') as sleep:
            self.assertEqual(m.fetch(session, 'GET', 'https://example.com').status_code, 200)
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_post_not_repeated(self):
        session = Mock()
        session.request.side_effect = m.requests.exceptions.Timeout()
        with self.assertRaises(m.requests.exceptions.Timeout):
            m.fetch(session, 'POST', 'https://example.com')
        self.assertEqual(session.request.call_count, 1)

    def test_blocked_not_retried(self):
        session = Mock()
        session.request.return_value = response(403)
        with self.assertRaises(m.BlockedError):
            m.fetch(session, 'GET', 'https://example.com')
        self.assertEqual(session.request.call_count, 1)

    def test_429_respects_retry_after(self):
        session = Mock()
        session.request.return_value = response(429, headers={'Retry-After': '7200'})
        with self.assertRaises(m.TemporaryError) as raised:
            m.fetch(session, 'GET', 'https://example.com')
        self.assertEqual(raised.exception.retry_after, 7200)
        self.assertEqual(session.request.call_count, 1)

    def test_blocked_cooldown_keeps_notification_history(self):
        m.save_state({'notified': ['2026-12-06']})
        with patch.object(m, 'crawl', side_effect=m.BlockedError('blocked')):
            self.assertEqual(m.main(), 1)
        state = m.load_state()
        self.assertEqual(state['notified'], ['2026-12-06'])
        self.assertEqual(state['health']['next_check_after'], '2026-11-19T22:00:00+09:00')
        with patch.object(m, 'crawl') as crawl:
            self.assertEqual(m.main(), 0)
            crawl.assert_not_called()
        self.assertEqual(json.loads(m.STATUS_FILE.read_text(encoding='utf-8'))['status'], 'deferred')

    def test_network_exhaustion_is_not_success(self):
        with patch.object(m, 'crawl', side_effect=m.requests.exceptions.Timeout()):
            self.assertEqual(m.main(), 1)
        self.assertEqual(json.loads(m.STATUS_FILE.read_text(encoding='utf-8'))['status'], 'network')

    def test_failed_notification_never_advances_state(self):
        m.save_state({'notified': []})
        with patch.object(m, 'crawl', return_value=[datetime.date(2026, 11, 22)]), patch.object(m, 'notify', side_effect=m.requests.exceptions.Timeout()):
            self.assertEqual(m.main(), 1)
        self.assertEqual(m.load_state()['notified'], [])

    def test_unobserved_month_is_preserved_and_closed_observed_slot_removed(self):
        m.save_state({'notified': ['2026-11-22', '2026-12-06']})
        def crawl():
            m.OBSERVED_MONTHS.add((2026, 11))
            return []
        with patch.object(m, 'crawl', side_effect=crawl):
            self.assertEqual(m.main(), 0)
        self.assertEqual(m.load_state()['notified'], ['2026-12-06'])

    def test_next_month_refusal_not_swallowed(self):
        r = response(body='振替可能な日程')
        r.url = 'https://example.com'
        with patch.object(m, 'login'), patch.object(m, 'fetch', return_value=r), patch.object(m, 'parse_sunday_slots', return_value=[]), patch.object(m, 'follow', side_effect=[(r, None, r.url)]*3 + [m.BlockedError('blocked')]):
            with self.assertRaises(m.BlockedError):
                m.crawl()

    def test_maintenance_is_explicitly_unconfirmed(self):
        class Midnight(FixedNow):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 19, 0, 5, tzinfo=m.JST)
        with patch.object(m.datetime, 'datetime', Midnight), patch.object(m, 'crawl') as crawl:
            self.assertEqual(m.main(), 0)
            crawl.assert_not_called()
        self.assertEqual(json.loads(m.STATUS_FILE.read_text(encoding='utf-8'))['status'], 'scheduled_pause')

    def test_notification_http_error_raises(self):
        with patch.object(m.requests, 'post', return_value=response(500)):
            with self.assertRaises(m.requests.exceptions.HTTPError):
                m.notify('test')


if __name__ == '__main__':
    unittest.main()
