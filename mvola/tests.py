from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import EXPECTED_COLUMNS
from .models import (
    InteropMvolaCbs,
    InteropMvolaMvola,
    InteropMvolaProcess,
    InteropMvolaReconciliation,
    OrphanMvolaProcessingHistory,
    ReconciliationRun,
)


def valid_report_bytes():
    header = ';'.join(EXPECTED_COLUMNS)
    row = '22/09/2026 07:05:20;7639903483;Completed;0387067680;0385344178;C;TEST;0;wallettobank;217000;300129339;300346339;file-id;WTB'
    return f'{header}\n{row}\n'.encode('utf-8')


class ExternalModelMappingTests(SimpleTestCase):
    def test_interop_models_are_unmanaged_and_use_existing_tables(self):
        expected_tables = {
            InteropMvolaProcess: 'interop_mvola_process',
            InteropMvolaCbs: 'interop_mvola_cbs',
            InteropMvolaMvola: 'interop_mvola_mvola',
            InteropMvolaReconciliation: 'interop_mvola_reconciliation',
        }

        for model, table_name in expected_tables.items():
            with self.subTest(model=model.__name__):
                self.assertFalse(model._meta.managed)
                self.assertEqual(model._meta.db_table, table_name)

        for model in (
            InteropMvolaCbs,
            InteropMvolaMvola,
            InteropMvolaReconciliation,
        ):
            with self.subTest(foreign_key_model=model.__name__):
                self.assertEqual(model._meta.get_field('process').column, 'process_id')


class MvolaReportImportTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings_override = override_settings(MVOLA_INPUT_DIR=Path(self.temp_dir.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.url = reverse('mvola:import')

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

    def upload(self, name='2026-09-22_reporting_PAMF.csv', content=None):
        return self.client.post(self.url, {
            'report': SimpleUploadedFile(
                name,
                content if content is not None else valid_report_bytes(),
                content_type='text/csv',
            ),
        })

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertRedirects(response, f'{reverse("accounts:login")}?next={self.url}')

    def test_viewer_is_forbidden_from_importing(self):
        self.login_with_role('Viewer')

        response = self.upload()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Path(self.temp_dir.name, '2026-09-22_reporting_PAMF.csv').exists())

    def test_backoffice_can_upload_valid_report_without_changing_original_name(self):
        self.login_with_role('Backoffice')

        response = self.upload()

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        saved = Path(self.temp_dir.name, '2026-09-22_reporting_PAMF.csv')
        self.assertEqual(saved.read_bytes(), valid_report_bytes())
        page = self.client.get(self.url)
        self.assertContains(page, '1 lignes, 1 transactions Completed')

    def test_admin_can_upload_valid_report(self):
        self.login_with_role('Admin')

        response = self.upload()

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertTrue(Path(self.temp_dir.name, '2026-09-22_reporting_PAMF.csv').exists())

    def test_invalid_names_and_paths_are_rejected_without_writing(self):
        self.login_with_role('Backoffice')
        invalid_names = (
            '2026-02-30_reporting_PAMF.csv',
            '2026-09-22_report.csv',
        )

        for name in invalid_names:
            with self.subTest(name=name):
                response = self.upload(name=name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.context['form'].errors['report']), 1)

        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_form_rejects_raw_path_components(self):
        from types import SimpleNamespace

        from .forms import MvolaReportUploadForm

        form = MvolaReportUploadForm()
        form.cleaned_data = {
            'report': SimpleNamespace(name='../2026-09-22_reporting_PAMF.csv'),
        }

        with self.assertRaisesRegex(ValidationError, 'sans chemin'):
            form.clean_report()

    def test_bad_headers_and_malformed_rows_are_rejected(self):
        self.login_with_role('Backoffice')
        bad_header = b'wrong;header\nvalue;value\n'
        bad_row = valid_report_bytes() + b'extra;column\n'

        header_response = self.upload(content=bad_header)
        malformed_response = self.upload(
            name='2026-09-23_reporting_PAMF.csv',
            content=bad_row,
        )

        self.assertContains(header_response, 'colonnes du CSV')
        self.assertContains(malformed_response, 'nombre de colonnes incorrect')
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_duplicate_report_is_never_overwritten(self):
        self.login_with_role('Backoffice')
        target = Path(self.temp_dir.name, '2026-09-22_reporting_PAMF.csv')
        original = b'original-content'
        target.write_bytes(original)

        response = self.upload()

        self.assertContains(response, 'existe déjà')
        self.assertEqual(target.read_bytes(), original)

    @override_settings(MVOLA_MAX_UPLOAD_SIZE=32)
    def test_oversized_report_is_rejected(self):
        self.login_with_role('Backoffice')

        response = self.upload(content=valid_report_bytes() + b'x' * 100)

        self.assertContains(response, 'dépasse la limite')
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])


class ReconciliationLaunchTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings_override = override_settings(MVOLA_INPUT_DIR=Path(self.temp_dir.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.url = reverse('mvola:launch_reconciliation')
        self.report_name = '2026-09-22_reporting_PAMF.csv'
        Path(self.temp_dir.name, self.report_name).write_bytes(valid_report_bytes())

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

        with patch('mvola.views.reconciliation', side_effect=reconcile) as service:
            response = self.client.post(self.url, {'filename': self.report_name})

        run = ReconciliationRun.objects.get()
        self.assertRedirects(response, reverse('mvola:import'))
        self.assertEqual(run.requested_by_id, user.pk)
        self.assertEqual(run.filename, self.report_name)
        self.assertEqual(run.status, ReconciliationRun.Status.SUCCEEDED)
        self.assertEqual(run.process_id, 42)
        service.assert_called_once_with(self.report_name, user.pk)

    def test_ajax_response_is_returned_only_after_synchronous_service_finishes(self):
        user = self.login_with_role('Backoffice')
        with patch('mvola.views.reconciliation', return_value={
            'status': 'success',
            'message': 'Persisté',
            'process_id': 73,
        }) as service:
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
            'message': 'Persisté',
            'process_id': 73,
            'finished': True,
        })
        self.assertEqual(run.requested_by_id, user.pk)
        service.assert_called_once_with(self.report_name, user.pk)

    def test_service_error_is_returned_after_marking_run_failed(self):
        self.login_with_role('Backoffice')
        with patch('mvola.views.reconciliation', return_value={
            'status': 'error',
            'message': 'No data found in CBS',
        }) as service:
            response = self.client.post(
                self.url,
                {'filename': self.report_name},
                HTTP_ACCEPT='application/json',
            )

        run = ReconciliationRun.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        self.assertEqual(response.json()['status'], 'failed')
        self.assertTrue(response.json()['finished'])
        self.assertEqual(run.status, ReconciliationRun.Status.FAILED)
        self.assertEqual(run.message, 'No data found in CBS')
        service.assert_called_once()

    def test_service_exception_is_returned_as_final_failure(self):
        self.login_with_role('Backoffice')
        with patch('mvola.views.reconciliation', side_effect=RuntimeError('CBS configuration missing')):
            response = self.client.post(
                self.url,
                {'filename': self.report_name},
                HTTP_ACCEPT='application/json',
            )

        run = ReconciliationRun.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        self.assertEqual(run.status, ReconciliationRun.Status.FAILED)
        self.assertIn('CBS configuration missing', response.json()['message'])

    def test_import_page_contains_nondismissible_reconciliation_modal(self):
        self.login_with_role('Backoffice')

        response = self.client.get(reverse('mvola:import'))

        self.assertContains(response, 'id="reconciliationModal"')
        self.assertContains(response, 'data-bs-backdrop="static"')
        self.assertContains(response, 'data-bs-keyboard="false"')
        self.assertContains(response, 'id="reconciliationClose" disabled')
        self.assertContains(response, "stateElement.textContent = 'En cours...'")
        self.assertContains(response, 'modal.hide()')
        self.assertNotContains(response, 'pollStatus')

    def test_completed_run_message_and_process_id_are_in_message_column(self):
        user = self.login_with_role('Backoffice')
        ReconciliationRun.objects.create(
            requested_by=user,
            filename=self.report_name,
            status=ReconciliationRun.Status.SUCCEEDED,
            message='Data successfully inserted into the database for the date: 2026-09-07',
            process_id=7,
        )

        response = self.client.get(reverse('mvola:import'))

        self.assertContains(
            response,
            '<td>\n    <div>Data successfully inserted into the database for the date: 2026-09-07</div>\n'
            '    <div class="small text-muted-debian">Processus #7</div>\n</td>',
            html=True,
        )

    def test_each_launch_creates_a_distinct_run_for_same_date(self):
        self.login_with_role('Backoffice')
        with patch('mvola.views.reconciliation', return_value={
            'status': 'success',
            'message': 'Done',
            'process_id': 1,
        }):
            self.client.post(self.url, {'filename': self.report_name})
            self.client.post(self.url, {'filename': self.report_name})

        self.assertEqual(ReconciliationRun.objects.count(), 2)

    def test_viewer_cannot_launch_a_run(self):
        self.login_with_role('Viewer')

        response = self.client.post(self.url, {'filename': self.report_name})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReconciliationRun.objects.exists())

    def test_missing_or_path_filename_is_rejected(self):
        self.login_with_role('Backoffice')

        response = self.client.post(self.url, {'filename': '../' + self.report_name})

        self.assertRedirects(response, reverse('mvola:import'))
        self.assertFalse(ReconciliationRun.objects.exists())


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
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
            'total_count': 12,
            'matched_count': 10,
            'orphan_mvola_count': 1,
            'orphan_pamf_count': 1,
        }]
        with patch('mvola.views.list_reconciliation_processes', return_value=processes) as repository:
            response = self.client.get(reverse('mvola:reconciliation_history'), {
                'date_from': '2026-09-01',
                'date_to': '2026-09-30',
                'process_id': '7',
                'status': 'orphan_mvola',
            })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Processus')
        self.assertContains(response, '10')
        self.assertContains(response, 'Orphelines MVOLA')
        repository.assert_called_once_with(
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            process_id=7,
            status='orphan_mvola',
        )

    def test_detail_shows_selected_source_rows_and_pagination(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        results = {
            'columns': (('TRANSID_MVOLA', 'Transaction MVOLA'), ('reconciliation_status', 'Statut')),
            'rows': [{'id': 99, 'TRANSID_MVOLA': '7639903483', 'reconciliation_status': 'orphan_mvola'}],
            'total': 51,
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value=results) as repository:
            response = self.client.get(reverse('mvola:reconciliation_detail', args=[7]), {
                'source': 'reconciliation',
                'status': 'orphan_mvola',
            })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '7639903483')
        self.assertContains(response, 'Page')
        self.assertEqual(response.context['total_pages'], 2)
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='orphan_mvola',
            transaction_id='',
            phone='',
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
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value={
                    'columns': (), 'rows': (), 'total': 0,
                }) as repository:
            response = self.client.get(reverse('mvola:reconciliation_detail', args=[7]))

        self.assertContains(response, 'Transactions MVOLA')
        self.assertContains(response, 'Transactions CBS')
        self.assertContains(response, 'Écarts et rapprochement')
        self.assertContains(response, 'Orphan MVOLA')
        self.assertContains(response, 'Orphan PAMF')
        self.assertContains(response, 'Rapprochées')
        self.assertEqual(response.context['source'], 'reconciliation')
        self.assertEqual(response.context['status'], 'orphan_mvola')
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='orphan_mvola',
            transaction_id=None,
            phone=None,
            amount=None,
            limit=50,
            offset=0,
        )

    def test_detail_filters_transaction_phone_and_amount(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value={
                    'columns': (), 'rows': (), 'total': 0,
                }) as repository:
            response = self.client.get(reverse('mvola:reconciliation_detail', args=[7]), {
                'source': 'reconciliation',
                'status': 'orphan_pamf',
                'transaction_id': '763990',
                'phone': '0345',
                'amount': '217000.50',
            })

        self.assertEqual(response.status_code, 200)
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='orphan_pamf',
            transaction_id='763990',
            phone='0345',
            amount=Decimal('217000.5000'),
            limit=50,
            offset=0,
        )
        self.assertIn('transaction_id=763990', response.context['status_tabs'][0]['url'])
        self.assertIn('phone=0345', response.context['status_tabs'][0]['url'])
        self.assertIn('amount=217000.5', response.context['status_tabs'][0]['url'])

    def test_empty_amount_does_not_leak_none_into_status_tab_urls(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value={
                    'columns': (), 'rows': (), 'total': 0,
                }):
            response = self.client.get(reverse('mvola:reconciliation_detail', args=[7]), {
                'source': 'reconciliation',
                'status': 'matched',
                'transaction_id': '',
                'phone': '',
                'amount': '',
            })

        self.assertEqual(response.status_code, 200)
        for tab in response.context['status_tabs']:
            self.assertNotIn('amount=None', tab['url'])
        self.assertNotIn('amount=None', response.context['pagination_query'])

    def test_export_fetches_every_row_ignoring_pagination(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        results = {
            'columns': (('TRANSID_MVOLA', 'Transaction MVOLA'), ('reconciliation_status', 'Statut')),
            'rows': [
                {'id': 1, 'TRANSID_MVOLA': 'A1', 'reconciliation_status': 'matched'},
                {'id': 2, 'TRANSID_MVOLA': 'A2', 'reconciliation_status': 'matched'},
            ],
            'total': 2,
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value=results) as repository:
            response = self.client.get(
                reverse('mvola:reconciliation_detail_export', args=[7]),
                {'source': 'reconciliation', 'status': 'matched'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        repository.assert_called_once_with(
            process_id=7,
            source='reconciliation',
            status='matched',
            transaction_id='',
            phone='',
            amount=None,
            limit=None,
            offset=0,
        )
        content = response.content.decode('utf-8-sig')
        self.assertIn('Transaction MVOLA;Statut', content)
        self.assertIn('A1;matched', content)
        self.assertIn('A2;matched', content)

    def test_export_includes_latest_orphan_status_column(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        results = {
            'columns': (('TRANSID_MVOLA', 'Transaction MVOLA'),),
            'rows': [{'id': 1, 'TRANSID_MVOLA': 'A1'}],
            'total': 1,
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value=results), \
                patch(
                    'mvola.views._latest_orphan_statuses',
                    return_value={1: 'Orphan mvola'},
                ):
            response = self.client.get(
                reverse('mvola:reconciliation_detail_export', args=[7]),
                {'source': 'reconciliation', 'status': 'orphan_mvola'},
            )

        content = response.content.decode('utf-8-sig')
        self.assertIn('Dernier statut', content)
        self.assertIn('A1;Orphan mvola', content)

    def test_export_rejects_invalid_filters_and_redirects_back(self):
        self.login_with_role('Backoffice')
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows') as repository:
            response = self.client.get(
                reverse('mvola:reconciliation_detail_export', args=[7]),
                {'source': 'reconciliation', 'status': 'matched', 'amount': 'not-a-number'},
            )

        self.assertRedirects(
            response,
            f"{reverse('mvola:reconciliation_detail', args=[7])}"
            f"?source=reconciliation&status=matched&amount=not-a-number",
            fetch_redirect_response=False,
        )
        repository.assert_not_called()

    def test_viewer_can_export_but_users_without_permission_cannot(self):
        process = {
            'id': 7,
            'user_id': 2,
            'status': 'completed',
            'inserted_at': datetime(2026, 9, 7, 9, 30),
            'transaction_date': date(2026, 9, 7),
        }
        get_user_model().objects.create_user(
            username='no-results-export',
            email='no-results-export@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        self.client.login(username='no-results-export', password='Long-local-pass-123!')

        with patch('mvola.views.get_reconciliation_process', return_value=process):
            response = self.client.get(reverse('mvola:reconciliation_detail_export', args=[7]))

        self.assertEqual(response.status_code, 403)

    def test_users_without_results_permission_cannot_open_history(self):
        get_user_model().objects.create_user(
            username='no-results',
            email='no-results@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        self.client.login(username='no-results', password='Long-local-pass-123!')

        response = self.client.get(reverse('mvola:reconciliation_history'))

        self.assertEqual(response.status_code, 403)


class OrphanMvolaActionTests(TestCase):
    """Uses schema_editor to create tables for the unmanaged interop_mvola_*
    models (InteropMvolaProcess, InteropMvolaReconciliation): they are not
    covered by Django migrations, so the test database does not have them
    unless we create them here for the duration of the test class.
    OrphanMvolaProcessingHistory is a regular managed model and already has
    its table from the mvola migrations."""

    @classmethod
    def setUpClass(cls):
        # SQLite refuses schema changes while FK checks are enabled inside an
        # atomic block, so this must happen before TestCase opens its
        # class-level transaction.
        connection.disable_constraint_checking()
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(InteropMvolaProcess)
            editor.create_model(InteropMvolaReconciliation)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(InteropMvolaReconciliation)
            editor.delete_model(InteropMvolaProcess)
        super().tearDownClass()
        connection.enable_constraint_checking()

    def setUp(self):
        self.process = InteropMvolaProcess.objects.create(
            user_id=1,
            status='completed',
            insert_date=timezone.now(),
            transaction_date=date(2026, 9, 7),
        )
        self.orphan_row = InteropMvolaReconciliation.objects.create(
            process=self.process,
            date_trans='2026-09-07',
            transid_mvola='7639903483',
            state='Completed',
            msisdn='0387067680',
            reconciliation_status='orphan_mvola',
        )
        self.matched_row = InteropMvolaReconciliation.objects.create(
            process=self.process,
            transid_mvola='999',
            reconciliation_status='matched',
        )

    def login_with_role(self, role_name):
        user = get_user_model().objects.create_user(
            username=f'orphan-{role_name.lower()}',
            email=f'orphan-{role_name.lower()}@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        user.groups.add(Group.objects.get(name=role_name))
        self.client.force_login(user)
        return user

    def test_viewer_can_open_orphan_modal_but_cannot_act(self):
        self.login_with_role('Viewer')
        with patch('mvola.views.get_transaction_by_requestID', return_value={
            'requestID': '7639903483',
            'RequestBody': '{"amount": 217000}',
            'RequestURL': '/wtb',
            'ResponseBody': '{"status": "ok"}',
        }) as lookup:
            response = self.client.get(reverse('mvola:orphan_detail', args=[self.orphan_row.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '7639903483')
        self.assertContains(response, '<pre class="small text-wrap mb-0">{&quot;amount&quot;: 217000}</pre>', html=True)
        self.assertNotContains(response, 'Régulariser cette transaction')
        lookup.assert_called_once_with('7639903483')

    def test_backoffice_sees_action_form_and_unmatched_cbs_lookup(self):
        self.login_with_role('Backoffice')
        with patch('mvola.views.get_transaction_by_requestID', return_value=None) as lookup:
            response = self.client.get(reverse('mvola:orphan_detail', args=[self.orphan_row.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Régulariser cette transaction')
        self.assertContains(response, 'Aucune transaction correspondante trouvée côté PAMF')
        self.assertContains(response, 'Aucune action enregistrée pour cette transaction.')
        lookup.assert_called_once_with('7639903483')

    def test_matched_row_cannot_be_opened_as_an_orphan(self):
        self.login_with_role('Viewer')

        response = self.client.get(reverse('mvola:orphan_detail', args=[self.matched_row.pk]))

        self.assertEqual(response.status_code, 404)

    def test_backoffice_can_record_a_processing_action(self):
        self.login_with_role('Backoffice')
        with patch('mvola.views.get_transaction_by_requestID', return_value=None):
            response = self.client.post(
                reverse('mvola:orphan_action', args=[self.orphan_row.pk]),
                {'status': 'processing', 'comments': 'Ticket ouvert côté PAMF.'},
            )

        self.assertEqual(response.status_code, 200)
        entry = OrphanMvolaProcessingHistory.objects.get(
            InteropMvolaReconciliation_id=self.orphan_row.pk,
        )
        self.assertEqual(entry.status, 'processing')
        self.assertEqual(entry.comments, 'Ticket ouvert côté PAMF.')
        self.assertFalse(entry.is_retried)
        self.assertContains(response, 'Ticket ouvert côté PAMF.')
        self.assertContains(response, 'En cours de traitement')

    def test_second_action_on_same_row_is_marked_as_retried(self):
        self.login_with_role('Backoffice')
        with patch('mvola.views.get_transaction_by_requestID', return_value=None):
            self.client.post(
                reverse('mvola:orphan_action', args=[self.orphan_row.pk]),
                {'status': 'processing', 'comments': ''},
            )
            response = self.client.post(
                reverse('mvola:orphan_action', args=[self.orphan_row.pk]),
                {'status': 'done', 'comments': 'Régularisé manuellement.'},
            )

        entries = list(
            OrphanMvolaProcessingHistory.objects.filter(
                InteropMvolaReconciliation_id=self.orphan_row.pk,
            ).order_by('processed_at')
        )
        self.assertEqual([entry.status for entry in entries], ['processing', 'done'])
        self.assertFalse(entries[0].is_retried)
        self.assertTrue(entries[1].is_retried)
        self.assertContains(response, 'Nouvelle tentative')

    def test_viewer_cannot_record_an_action(self):
        self.login_with_role('Viewer')

        response = self.client.post(
            reverse('mvola:orphan_action', args=[self.orphan_row.pk]),
            {'status': 'processing', 'comments': ''},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(OrphanMvolaProcessingHistory.objects.exists())

    def test_action_on_matched_row_is_rejected(self):
        self.login_with_role('Backoffice')

        response = self.client.post(
            reverse('mvola:orphan_action', args=[self.matched_row.pk]),
            {'status': 'processing', 'comments': ''},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(OrphanMvolaProcessingHistory.objects.exists())


class ReconciliationDetailLatestOrphanStatusTests(TestCase):
    @classmethod
    def setUpClass(cls):
        connection.disable_constraint_checking()
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(InteropMvolaProcess)
            editor.create_model(InteropMvolaReconciliation)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(InteropMvolaReconciliation)
            editor.delete_model(InteropMvolaProcess)
        super().tearDownClass()
        connection.enable_constraint_checking()

    def setUp(self):
        self.process = InteropMvolaProcess.objects.create(
            user_id=1,
            status='completed',
            insert_date=timezone.now(),
            transaction_date=date(2026, 9, 7),
        )
        self.orphan_row = InteropMvolaReconciliation.objects.create(
            process=self.process,
            transid_mvola='7639903483',
            reconciliation_status='orphan_mvola',
        )
        OrphanMvolaProcessingHistory.objects.create(
            InteropMvolaReconciliation=self.orphan_row,
            status='processing',
        )
        OrphanMvolaProcessingHistory.objects.create(
            InteropMvolaReconciliation=self.orphan_row,
            status='done',
        )

        user = get_user_model().objects.create_user(
            username='backoffice-latest-status',
            email='backoffice-latest-status@example.com',
            password='Long-local-pass-123!',
            is_active=True,
        )
        user.groups.add(Group.objects.get(name='Backoffice'))
        self.client.force_login(user)

    def test_latest_orphan_status_column_is_shown_for_orphan_mvola_rows(self):
        results = {
            'columns': (('TRANSID_MVOLA', 'Transaction MVOLA'), ('reconciliation_status', 'Statut')),
            'rows': [{
                'id': self.orphan_row.pk,
                'TRANSID_MVOLA': '7639903483',
                'reconciliation_status': 'orphan_mvola',
            }],
            'total': 1,
        }
        process = {
            'id': self.process.pk,
            'user_id': 1,
            'status': 'completed',
            'inserted_at': timezone.now(),
            'transaction_date': date(2026, 9, 7),
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value=results):
            response = self.client.get(
                reverse('mvola:reconciliation_detail', args=[self.process.pk]),
                {'source': 'reconciliation', 'status': 'orphan_mvola'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dernier statut')
        self.assertContains(response, 'Done')

    def test_latest_orphan_status_column_is_absent_for_other_sources(self):
        results = {
            'columns': (('TRANSID_MVOLA', 'Transaction MVOLA'),),
            'rows': [{'id': self.orphan_row.pk, 'TRANSID_MVOLA': '7639903483'}],
            'total': 1,
        }
        process = {
            'id': self.process.pk,
            'user_id': 1,
            'status': 'completed',
            'inserted_at': timezone.now(),
            'transaction_date': date(2026, 9, 7),
        }
        with patch('mvola.views.get_reconciliation_process', return_value=process), \
                patch('mvola.views.get_process_result_rows', return_value=results):
            response = self.client.get(
                reverse('mvola:reconciliation_detail', args=[self.process.pk]),
                {'source': 'mvola'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Dernier statut')


