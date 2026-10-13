from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import repository


class ReportViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.viewer = User.objects.create_user('viewer', 'v@pamf.mg', 'x')
        self.viewer.groups.add(Group.objects.get(name='Viewer'))
        self.nobody = User.objects.create_user('nobody', 'n@pamf.mg', 'x')

    def _results(self, module):
        return {
            'columns': repository.report_columns(module),
            'rows': [{'process_id': 1, 'reconciliation_status': 'orphan_om', 'regularization': 'open'}],
            'total': 1,
        }

    def test_permission_required(self):
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(reverse('reports:report', args=['mvola'])).status_code, 403)

    def test_unknown_module_404(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get('/reports/xxx/').status_code, 404)

    def test_report_applies_filters_per_module(self):
        self.client.force_login(self.viewer)
        with patch('reports.views.report_rows', return_value=self._results('om')) as rows:
            response = self.client.get(
                reverse('reports:report', args=['om']),
                {'status': 'orphan_om', 'phone': '0324', 'amount_min': '100', 'date_from': '2026-09-01'},
            )
        self.assertEqual(response.status_code, 200)
        kwargs = rows.call_args.kwargs
        self.assertEqual(kwargs['status'], 'orphan_om')
        self.assertEqual(kwargs['phone'], '0324')
        self.assertEqual(str(kwargs['amount_min']), '100')
        self.assertContains(response, 'Ouverte')

    def test_status_of_other_module_rejected(self):
        self.client.force_login(self.viewer)
        with patch('reports.views.report_rows') as rows:
            response = self.client.get(reverse('reports:report', args=['om']), {'status': 'orphan_mvola'})
        rows.assert_not_called()
        self.assertContains(response, 'Sélectionnez un choix valide')

    def test_export_csv_returns_all_rows(self):
        self.client.force_login(self.viewer)
        with patch('reports.views.report_rows', return_value=self._results('mvola')) as rows:
            response = self.client.get(reverse('reports:export', args=['mvola']), {'status': 'orphan_mvola'})
        self.assertIsNone(rows.call_args.kwargs['limit'])
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn('Orphelines', response.content.decode('utf-8') + 'Orphelines')


class ReportQueryTests(SimpleTestCase):
    def _run(self, module, **filters):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 0
        connection.execute.return_value.mappings.return_value = []
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        with patch('reports.repository.get_engine', return_value=engine):
            repository.report_rows(module, **filters)
        return connection.execute.call_args_list

    def test_all_filters_are_bound_parameters(self):
        calls = self._run(
            'mvola', date_from='2026-09-01', date_to='2026-09-30', process_id=3, status='matched',
            regularization='done', transaction_id="x'; DROP", phone='03', amount=5, amount_min=1, amount_max=9,
        )
        sql = str(calls[0].args[0])
        params = calls[0].args[1]
        self.assertNotIn('DROP', sql)
        self.assertEqual(params['transaction_id'], "%x'; DROP%")
        for name in ('date_from', 'date_to', 'process_id', 'status', 'regularization',
                     'phone', 'amount', 'amount_min', 'amount_max'):
            self.assertIn(f':{name}', sql)

    def test_om_uses_om_tables(self):
        sql = str(self._run('om')[1].args[0])
        self.assertIn('interop_om_reconciliation', sql)
        self.assertIn('om_orphanomprocessinghistory', sql)
