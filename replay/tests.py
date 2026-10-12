from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from . import service
from .models import ReplayLog, ReplayRequest


@override_settings(REPLAY_API_KEY='k', REPLAY_API_BASE_URL='http://x/api')
class ReplayServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.maker = User.objects.create_user('maker', 'maker@pamf.mg', 'x')
        self.checker = User.objects.create_user('checker', 'checker@pamf.mg', 'x')

    def _ok(self):
        return ('http://x/api/maker', 200, '{}', True, '')

    def _maker(self):
        with patch('replay.service._post', return_value=self._ok()):
            return service.submit_maker(
                source='mvola', reconciliation_id=1, request_id='123', amount=500, user=self.maker)

    def test_maker_then_checker(self):
        replay, ok, _ = self._maker()
        self.assertTrue(ok)
        with patch('replay.service._post', return_value=self._ok()):
            replay, ok, _ = service.submit_checker(replay_id=replay.pk, user=self.checker)
        self.assertTrue(ok)
        self.assertEqual(replay.status, ReplayRequest.Status.COMPLETED)
        self.assertEqual(ReplayLog.objects.count(), 2)

    def test_maker_cannot_check(self):
        replay, _, _ = self._maker()
        with self.assertRaises(service.ReplayError):
            service.submit_checker(replay_id=replay.pk, user=self.maker)

    def test_duplicate_maker_refused(self):
        self._maker()
        with self.assertRaises(service.ReplayError):
            self._maker()

    def test_maker_failure_allows_retry(self):
        with patch('replay.service._post', return_value=('u', 500, 'err', False, 'HTTP 500')):
            replay, ok, _ = service.submit_maker(
                source='mvola', reconciliation_id=1, request_id='123', amount=500, user=self.maker)
        self.assertFalse(ok)
        self.assertEqual(replay.status, ReplayRequest.Status.MAKER_FAILED)
        self.assertTrue(self._maker()[1])
