import io
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import xlwt
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import OM_COLUMNS
from .models import (
    InteropOmCbs,
    InteropOmOm,
    InteropOmProcess,
    InteropOmReconciliation,
    OrphanOmProcessingHistory,
    ReconciliationRun,
)


def build_om_xls_bytes(rows):
    """Build a minimal .xls file: no fixed-position header row, just the
    positional rows service_om.read_om_transaction() expects (see om/forms.py)."""
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('Sheet1')
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            sheet.write(row_index, col_index, value)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def valid_om_report_bytes():
    junk_row = ['Relevé de vos opérations'] + [''] * 16
    success_row = [
        1, '12/09/2026', '22:21:08', 'MP260912.1', 'Merchant Payment', 'Transaction',
        'Succès', '', '0324429507', 'Wallet', '', '0378650522', 'Normal', '', '0', 100, 0,
    ]
    return build_om_xls_bytes([junk_row, success_row])


class ExternalModelMappingTests(SimpleTestCase):
    def test_interop_models_are_unmanaged_and_use_existing_tables(self):
        expected_tables = {
            InteropOmProcess: 'interop_om_process',
            InteropOmCbs: 'interop_om_cbs',
            InteropOmOm: 'interop_om_om',
            InteropOmReconciliation: 'interop_om_reconciliation',
        }

        for model, table_name in expected_tables.items():
            with self.subTest(model=model.__name__):
                self.assertFalse(model._meta.managed)
                self.assertEqual(model._meta.db_table, table_name)

        for model in (InteropOmCbs, InteropOmOm, InteropOmReconciliation):
            with self.subTest(foreign_key_model=model.__name__):
                self.assertEqual(model._meta.get_field('process').column, 'process_id')


class OmReportImportTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings_override = override_settings(OM_INPUT_DIR=Path(self.temp_dir.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.url = reverse('om:import')

    def login_with_role(self, role_name):
        user = get_user_model().objects.create_user(
            username=f'user-{role_name.lower()}',
            email=f'{role_name.lower()}@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        user.groups.add(Group.objects.get(name=role_name))
        self.client.force_login(user)
        return user

    def upload(self, name='Daily-ChannelUserTransactionReport-0324660679-20260912.xls', content=None):
        return self.client.post(self.url, {
            'report': SimpleUploadedFile(
                name,
                content if content is not None else valid_om_report_bytes(),
                content_type='application/vnd.ms-excel',
            ),
        })

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertRedirects(response, f'{reverse("accounts:login")}?next={self.url}')

    def test_viewer_is_forbidden_from_importing(self):
        self.login_with_role('Viewer')

        response = self.upload()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Path(self.temp_dir.name, 'Daily-ChannelUserTransactionReport-0324660679-20260912.xls').exists()
        )

    def test_backoffice_can_upload_valid_report_without_changing_original_name(self):
        self.login_with_role('Backoffice')

        response = self.upload()

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        saved = Path(self.temp_dir.name, 'Daily-ChannelUserTransactionReport-0324660679-20260912.xls')
        self.assertEqual(saved.read_bytes(), valid_om_report_bytes())
        page = self.client.get(self.url)
        self.assertContains(page, '1 lignes, 1 transactions Succès')

    def test_admin_can_upload_valid_report(self):
        self.login_with_role('Admin')

        response = self.upload()

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertTrue(
            Path(self.temp_dir.name, 'Daily-ChannelUserTransactionReport-0324660679-20260912.xls').exists()
        )

    def test_invalid_names_and_paths_are_rejected_without_writing(self):
        self.login_with_role('Backoffice')
        invalid_names = (
            'Daily-ChannelUserTransactionReport-0324660679-20260230.xls',
            'Daily-ChannelUserTransactionReport-0324660679-20260912.xlsx',
            'Daily-ChannelUserTransactionReport-9999999999-20260912.xls',
        )

        for name in invalid_names:
            with self.subTest(name=name):
                response = self.upload(name=name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.context['form'].errors['report']), 1)

        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_form_rejects_raw_path_components(self):
        from types import SimpleNamespace

        from .forms import OmReportUploadForm

        form = OmReportUploadForm()
        form.cleaned_data = {
            'report': SimpleNamespace(
                name='../Daily-ChannelUserTransactionReport-0324660679-20260912.xls',
            ),
        }

        with self.assertRaisesRegex(ValidationError, 'sans chemin'):
            form.clean_report()

    def test_unreadable_file_is_rejected(self):
        self.login_with_role('Backoffice')

        response = self.upload(content=b'not an excel file at all')

        self.assertContains(response, 'Orange Money .xls lisible')
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_report_without_any_success_row_is_rejected(self):
        self.login_with_role('Backoffice')
        junk_row = ['Relevé de vos opérations'] + [''] * 16
        failed_row = [
            1, '12/09/2026', '22:21:08', 'MP1', 'Merchant Payment', 'Transaction',
            'Echec', '', '0324429507', 'Wallet', '', '0378650522', 'Normal', '', '0', 0, 0,
        ]
        content = build_om_xls_bytes([junk_row, failed_row])

        response = self.upload(content=content)

        self.assertContains(response, 'aucune transaction Succès')
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_report_with_wrong_column_count_is_rejected(self):
        self.login_with_role('Backoffice')
        content = build_om_xls_bytes([[1, 2, 3]])
        self.assertNotEqual(len(OM_COLUMNS), 3)

        response = self.upload(content=content)

        self.assertContains(response, 'Orange Money .xls lisible')
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_duplicate_report_is_never_overwritten(self):
        self.login_with_role('Backoffice')
        target = Path(self.temp_dir.name, 'Daily-ChannelUserTransactionReport-0324660679-20260912.xls')
        original = b'original-content'
        target.write_bytes(original)

        response = self.upload()

        self.assertContains(response, 'existe déjà')
        self.assertEqual(target.read_bytes(), original)

    @override_settings(OM_MAX_UPLOAD_SIZE=32)
    def test_oversized_report_is_rejected(self):
        self.login_with_role('Backoffice')

        response = self.upload(content=valid_om_report_bytes() + b'x' * 100)

        self.assertContains(response, 'dépasse la limite')
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])


class ReconciliationLaunchTests(TestCase):
    """Uses schema_editor to create tables for the unmanaged interop_om_*
    models (InteropOmProcess, InteropOmReconciliation): they are not covered
    by Django migrations, so the test database does not have them unless we
    create them here for the duration of the test class."""

    @classmethod
    def setUpClass(cls):
        connection.disable_constraint_checking()
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(InteropOmProcess)
            editor.create_model(InteropOmReconciliation)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(InteropOmReconciliation)
            editor.delete_model(InteropOmProcess)
        super().tearDownClass()
        connection.enable_constraint_checking()

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings_override = override_settings(OM_INPUT_DIR=Path(self.temp_dir.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.url = reverse('om:launch_reconciliation')
        self.report_name = 'Daily-ChannelUserTransactionReport-0324660679-20260912.xls'
        Path(self.temp_dir.name, self.report_name).write_bytes(valid_om_report_bytes())

    def login_with_role(self, role_name):
        user = get_user_model().objects.create_user(
            username=f'runner-{role_name.lower()}',
            email=f'runner-{role_name.lower()}@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        user.groups.add(Group.objects.get(name=role_name))
        self.client.force_login(user)
        return user

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.post(self.url, {'filename': self.report_name})

        self.assertRedirects(response, f'{reverse("accounts:login")}?next={self.url}')

    def test_viewer_is_forbidden_from_launching(self):
        self.login_with_role('Viewer')

        with patch('om.views.reconciliation') as service:
            response = self.client.post(self.url, {'filename': self.report_name})

        self.assertEqual(response.status_code, 403)
        service.assert_not_called()

    def test_get_is_not_allowed(self):
        self.login_with_role('Backoffice')

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_invalid_filename_is_rejected_without_calling_the_service(self):
        self.login_with_role('Backoffice')

        with patch('om.views.reconciliation') as service:
            response = self.client.post(
                self.url,
                {'filename': '../etc/passwd'},
                HTTP_ACCEPT='application/json',
            )

        self.assertEqual(response.status_code, 400)
        service.assert_not_called()

    def test_missing_report_file_is_rejected(self):
        self.login_with_role('Backoffice')
        missing_name = 'Daily-ChannelUserTransactionReport-0324660679-20260913.xls'

        with patch('om.views.reconciliation') as service:
            response = self.client.post(
                self.url,
                {'filename': missing_name},
                HTTP_ACCEPT='application/json',
            )

        self.assertEqual(response.status_code, 404)
        service.assert_not_called()

    def test_backoffice_waits_for_synchronous_service_result(self):
        user = self.login_with_role('Backoffice')

        def reconcile(filename, userid):
            run = ReconciliationRun.objects.get()
            self.assertEqual(run.status, ReconciliationRun.Status.RUNNING)
            self.assertEqual((filename, userid), (self.report_name, user.pk))
            return {
                'status': 'success',
                'message': 'Rapprochement terminé.',
                'process_id': 42,
            }

        with patch('om.views.reconciliation', side_effect=reconcile) as service:
            response = self.client.post(self.url, {'filename': self.report_name})

        run = ReconciliationRun.objects.get()
        self.assertRedirects(response, reverse('om:import'))
        self.assertEqual(run.requested_by_id, user.pk)
        self.assertEqual(run.filename, self.report_name)
        self.assertEqual(run.status, ReconciliationRun.Status.SUCCEEDED)
        self.assertEqual(run.process_id, 42)
        service.assert_called_once_with(self.report_name, user.pk)

    def test_missing_process_id_is_recovered_from_transaction_date(self):
        """service_om.reconciliation() does not return process_id yet (known
        bug, documented in CLAUDE_OM.md): the view must recover it by looking
        up the newest interop_om_process row for the report's transaction_date."""
        self.login_with_role('Backoffice')
        process = InteropOmProcess.objects.create(
            user_id=1,
            status='completed',
            insert_date=timezone.now(),
            transaction_date=date(2026, 9, 12),
        )

        with patch('om.views.reconciliation', return_value={
            'status': 'success',
            'message': 'Data inserted successfully.',
        }):
            response = self.client.post(self.url, {'filename': self.report_name})

        run = ReconciliationRun.objects.get()
        self.assertRedirects(response, reverse('om:import'))
        self.assertEqual(run.status, ReconciliationRun.Status.SUCCEEDED)
        self.assertEqual(run.process_id, process.id)

    def test_orphan_rows_are_copied_to_processing_history(self):
        self.login_with_role('Backoffice')
        process = InteropOmProcess.objects.create(
            user_id=1,
            status='completed',
            insert_date=timezone.now(),
            transaction_date=date(2026, 9, 12),
        )
        InteropOmReconciliation.objects.create(
            process=process,
            trx_id='OM1',
            reconciliation_status='orphan_om',
        )
        InteropOmReconciliation.objects.create(
            process=process,
            trx_id='OM2',
            reconciliation_status='matched',
        )

        with patch('om.views.reconciliation', return_value={
            'status': 'success',
            'message': 'Data inserted successfully.',
            'process_id': process.id,
        }):
            response = self.client.post(self.url, {'filename': self.report_name})

        run = ReconciliationRun.objects.get()
        self.assertRedirects(response, reverse('om:import'))
        self.assertEqual(run.status, ReconciliationRun.Status.SUCCEEDED)
        self.assertIn('1 écart(s) ajouté(s)', run.message)
        self.assertEqual(
            OrphanOmProcessingHistory.objects.filter(InteropOmReconciliation__process=process).count(),
            1,
        )

    def test_service_failure_marks_run_as_failed(self):
        self.login_with_role('Backoffice')

        with patch('om.views.reconciliation', return_value={
            'status': 'error',
            'message': 'CBS unavailable.',
        }):
            response = self.client.post(self.url, {'filename': self.report_name})

        run = ReconciliationRun.objects.get()
        self.assertRedirects(response, reverse('om:import'))
        self.assertEqual(run.status, ReconciliationRun.Status.FAILED)
        self.assertIsNone(run.process_id)

    def test_ajax_response_is_returned_only_after_synchronous_service_finishes(self):
        user = self.login_with_role('Backoffice')
        with patch('om.views.reconciliation', return_value={
            'status': 'success',
            'message': 'Persisté',
            'process_id': 73,
        }):
            response = self.client.post(
                self.url,
                {'filename': self.report_name},
                HTTP_ACCEPT='application/json',
            )

        run = ReconciliationRun.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'ok': True,
            'status': 'succeeded',
            'label': 'Terminé',
            'run_id': run.pk,
            'message': 'Persisté 0 écart(s) ajouté(s) à l’historique de régularisation.',
            'process_id': 73,
            'finished': True,
        })


class ReconciliationResultsTests(TestCase):
    def login_with_role(self, role_name):
        user = get_user_model().objects.create_user(
            username=f'results-{role_name.lower()}',
            email=f'results-{role_name.lower()}@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        user.groups.add(Group.objects.get(name=role_name))
        self.client.force_login(user)
        return user

    def test_history_filters_are_passed_to_the_external_repository(self):
        self.login_with_role('Viewer')
        processes = [{
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 12, 9, 30),
            'transaction_date': date(2026, 9, 12),
            'total_count': 12,
            'matched_count': 10,
            'orphan_om_count': 1,
            'orphan_pamf_count': 1,
        }]
        with patch('om.views.list_reconciliation_processes', return_value=processes) as repository:
            response = self.client.get(reverse('om:reconciliation_history'), {
                'date_from': '2026-09-01',
                'date_to': '2026-09-30',
                'process_id': '7',
                'status': 'orphan_om',
            })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Processus')
        self.assertContains(response, '10')
        self.assertContains(response, 'Orphelines OM')
        repository.assert_called_once_with(
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            process_id=7,
            status='orphan_om',
        )

    def test_detail_shows_selected_source_rows_and_pagination(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 12, 9, 30),
            'transaction_date': date(2026, 9, 12),
        }
        results = {
            'columns': (('trx_id', 'Transaction OM / CBS'), ('reconciliation_status', 'Statut')),
            'rows': [{'id': 99, 'trx_id': 'OM12345', 'reconciliation_status': 'orphan_om'}],
            'total': 51,
        }
        with patch('om.views.get_reconciliation_process', return_value=process), \
                patch('om.views.get_process_result_rows', return_value=results) as repository:
            response = self.client.get(reverse('om:reconciliation_detail', args=[7]), {
                'source': 'reconciliation',
                'status': 'orphan_om',
            })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'OM12345')
        self.assertContains(response, 'Page')
        self.assertEqual(response.context['total_pages'], 2)
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='orphan_om',
            transaction_id='',
            account='',
            amount=None,
            limit=50,
            offset=0,
        )

    def test_detail_has_three_source_tabs_and_three_reconciliation_status_tabs(self):
        self.login_with_role('Viewer')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 12, 9, 30),
            'transaction_date': date(2026, 9, 12),
        }
        with patch('om.views.get_reconciliation_process', return_value=process), \
                patch('om.views.get_process_result_rows', return_value={
                    'columns': (), 'rows': (), 'total': 0,
                }) as repository:
            response = self.client.get(reverse('om:reconciliation_detail', args=[7]))

        self.assertContains(response, 'Transactions Orange Money')
        self.assertContains(response, 'Transactions CBS')
        self.assertContains(response, 'Écarts et rapprochement')
        self.assertContains(response, 'Orphan OM')
        self.assertContains(response, 'Orphan PAMF')
        self.assertContains(response, 'Rapprochées')
        self.assertEqual(response.context['source'], 'reconciliation')
        self.assertEqual(response.context['status'], 'orphan_om')
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='orphan_om',
            transaction_id=None,
            account=None,
            amount=None,
            limit=50,
            offset=0,
        )

    def test_detail_filters_transaction_account_and_amount(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 12, 9, 30),
            'transaction_date': date(2026, 9, 12),
        }
        with patch('om.views.get_reconciliation_process', return_value=process), \
                patch('om.views.get_process_result_rows', return_value={
                    'columns': (), 'rows': (), 'total': 0,
                }) as repository:
            response = self.client.get(reverse('om:reconciliation_detail', args=[7]), {
                'source': 'reconciliation',
                'status': 'orphan_pamf',
                'transaction_id': 'OM123',
                'account': 'pseudo42',
                'amount': '100.50',
            })

        self.assertEqual(response.status_code, 200)
        from decimal import Decimal
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='orphan_pamf',
            transaction_id='OM123',
            account='pseudo42',
            amount=Decimal('100.5000'),
            limit=50,
            offset=0,
        )

    def test_export_fetches_every_row_ignoring_pagination(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 12, 9, 30),
            'transaction_date': date(2026, 9, 12),
        }
        results = {
            'columns': (('trx_id', 'Transaction OM / CBS'), ('reconciliation_status', 'Statut')),
            'rows': [
                {'id': 1, 'trx_id': 'A1', 'reconciliation_status': 'matched'},
                {'id': 2, 'trx_id': 'A2', 'reconciliation_status': 'matched'},
            ],
            'total': 2,
        }
        with patch('om.views.get_reconciliation_process', return_value=process), \
                patch('om.views.get_process_result_rows', return_value=results) as repository:
            response = self.client.get(
                reverse('om:reconciliation_detail_export', args=[7]),
                {'source': 'reconciliation', 'status': 'matched'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='matched',
            transaction_id='',
            account='',
            amount=None,
            limit=None,
            offset=0,
        )
        content = response.content.decode('utf-8-sig')
        self.assertIn('Transaction OM / CBS;Statut', content)
        self.assertIn('A1;matched', content)
        self.assertIn('A2;matched', content)

    def test_export_rejects_invalid_filters_and_redirects_back(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 12, 9, 30),
            'transaction_date': date(2026, 9, 12),
        }
        with patch('om.views.get_reconciliation_process', return_value=process), \
                patch('om.views.get_process_result_rows') as repository:
            response = self.client.get(
                reverse('om:reconciliation_detail_export', args=[7]),
                {'source': 'reconciliation', 'status': 'matched', 'amount': 'not-a-number'},
            )

        self.assertRedirects(
            response,
            f"{reverse('om:reconciliation_detail', args=[7])}"
            f"?source=reconciliation&status=matched&amount=not-a-number",
            fetch_redirect_response=False,
        )
        repository.assert_not_called()

    def test_users_without_results_permission_cannot_open_history(self):
        get_user_model().objects.create_user(
            username='no-results-om',
            email='no-results-om@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        self.client.login(username='no-results-om', password='Long-local-pass-123!')

        response = self.client.get(reverse('om:reconciliation_history'))

        self.assertEqual(response.status_code, 403)


class OrphanOmActionTests(TestCase):
    """Uses schema_editor to create tables for the unmanaged interop_om_*
    models (InteropOmProcess, InteropOmReconciliation): they are not covered
    by Django migrations, so the test database does not have them unless we
    create them here for the duration of the test class.
    OrphanOmProcessingHistory is a regular managed model and already has its
    table from the om migrations."""

    @classmethod
    def setUpClass(cls):
        connection.disable_constraint_checking()
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(InteropOmProcess)
            editor.create_model(InteropOmReconciliation)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(InteropOmReconciliation)
            editor.delete_model(InteropOmProcess)
        super().tearDownClass()
        connection.enable_constraint_checking()

    def setUp(self):
        self.process = InteropOmProcess.objects.create(
            user_id=1,
            status='completed',
            insert_date=timezone.now(),
            transaction_date=date(2026, 9, 12),
        )
        self.orphan_row = InteropOmReconciliation.objects.create(
            process=self.process,
            date='12/09/2026',
            trx_id='OM7639903483',
            status='Succès',
            pseudo='pseudo42',
            reconciliation_status='orphan_om',
        )
        self.matched_row = InteropOmReconciliation.objects.create(
            process=self.process,
            trx_id='OM999',
            reconciliation_status='matched',
        )

    def login_with_role(self, role_name):
        user = get_user_model().objects.create_user(
            username=f'orphan-om-{role_name.lower()}',
            email=f'orphan-om-{role_name.lower()}@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        user.groups.add(Group.objects.get(name=role_name))
        self.client.force_login(user)
        return user

    def test_viewer_can_open_orphan_modal_but_cannot_act(self):
        self.login_with_role('Viewer')
        with patch('om.views.get_transaction_by_requestID', return_value={
            'requestID': 'OM7639903483',
            'RequestBody': '{"amount": 100}',
            'RequestURL': '/wtb',
            'ResponseBody': '{"status": "ok"}',
        }) as lookup:
            response = self.client.get(reverse('om:orphan_detail', args=[self.orphan_row.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'OM7639903483')
        self.assertContains(response, '<pre class="small text-wrap mb-0">{&quot;amount&quot;: 100}</pre>', html=True)
        self.assertNotContains(response, 'Régulariser cette transaction')
        lookup.assert_called_once_with('OM7639903483')

    def test_backoffice_sees_action_form_and_unmatched_cbs_lookup(self):
        self.login_with_role('Backoffice')
        with patch('om.views.get_transaction_by_requestID', return_value=None) as lookup:
            response = self.client.get(reverse('om:orphan_detail', args=[self.orphan_row.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Régulariser cette transaction')
        self.assertContains(response, 'Aucune transaction correspondante trouvée côté PAMF')
        self.assertContains(response, 'Aucune action enregistrée pour cette transaction.')
        lookup.assert_called_once_with('OM7639903483')

    def test_matched_row_cannot_be_opened_as_an_orphan(self):
        self.login_with_role('Viewer')

        response = self.client.get(reverse('om:orphan_detail', args=[self.matched_row.pk]))

        self.assertEqual(response.status_code, 404)

    def test_backoffice_can_record_a_processing_action(self):
        self.login_with_role('Backoffice')
        with patch('om.views.get_transaction_by_requestID', return_value=None):
            response = self.client.post(
                reverse('om:orphan_action', args=[self.orphan_row.pk]),
                {'status': 'processing', 'comments': 'Ticket ouvert côté PAMF.'},
            )

        self.assertEqual(response.status_code, 200)
        entry = OrphanOmProcessingHistory.objects.get(
            InteropOmReconciliation_id=self.orphan_row.pk,
        )
        self.assertEqual(entry.status, 'processing')
        self.assertEqual(entry.comments, 'Ticket ouvert côté PAMF.')
        self.assertFalse(entry.is_retried)
        self.assertContains(response, 'Ticket ouvert côté PAMF.')
        self.assertContains(response, 'En cours de traitement')

    def test_second_action_on_same_row_is_marked_as_retried(self):
        self.login_with_role('Backoffice')
        with patch('om.views.get_transaction_by_requestID', return_value=None):
            self.client.post(
                reverse('om:orphan_action', args=[self.orphan_row.pk]),
                {'status': 'processing', 'comments': ''},
            )
            response = self.client.post(
                reverse('om:orphan_action', args=[self.orphan_row.pk]),
                {'status': 'done', 'comments': 'Régularisé manuellement.'},
            )

        entries = list(
            OrphanOmProcessingHistory.objects.filter(
                InteropOmReconciliation_id=self.orphan_row.pk,
            ).order_by('processed_at')
        )
        self.assertEqual([entry.status for entry in entries], ['processing', 'done'])
        self.assertFalse(entries[0].is_retried)
        self.assertTrue(entries[1].is_retried)
        self.assertContains(response, 'Nouvelle tentative')

    def test_viewer_cannot_record_an_action(self):
        self.login_with_role('Viewer')

        response = self.client.post(
            reverse('om:orphan_action', args=[self.orphan_row.pk]),
            {'status': 'processing', 'comments': ''},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(OrphanOmProcessingHistory.objects.exists())

    def test_action_on_matched_row_is_rejected(self):
        self.login_with_role('Backoffice')

        response = self.client.post(
            reverse('om:orphan_action', args=[self.matched_row.pk]),
            {'status': 'processing', 'comments': ''},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(OrphanOmProcessingHistory.objects.exists())
