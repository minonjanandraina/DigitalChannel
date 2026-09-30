import csv
from datetime import datetime
import logging
import math
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from sqlalchemy.exc import SQLAlchemyError

from service_mvola import get_transaction_by_requestID, reconciliation

from .forms import FILENAME_PATTERN, MvolaReportUploadForm, OrphanActionForm
from .forms import ReconciliationDetailFilterForm, ReconciliationHistoryFilterForm
from .models import (
    InteropMvolaCbs,
    InteropMvolaMvola,
    InteropMvolaReconciliation,
    OrphanMvolaProcessingHistory,
    ReconciliationRun,
)
from .orphan_history import copy_orphan_processing_history
from .results_repository import (
    RESULT_SOURCES,
    get_process_result_rows,
    get_reconciliation_process,
    list_reconciliation_processes,
)
from .storage import ReportAlreadyExists, store_report

logger = logging.getLogger(__name__)
RESULT_PAGE_SIZE = 50
REPORTS_PAGE_SIZE = 5


def _import_context(request, form):
    input_dir = Path(settings.MVOLA_INPUT_DIR)
    reports = sorted(
        (
            path.name for path in input_dir.glob('*_reporting_PAMF.csv')
            if path.is_file() and path.resolve().parent == input_dir
        ),
        reverse=True,
    ) if input_dir.is_dir() else []
    reports_paginator = Paginator(reports, REPORTS_PAGE_SIZE)
    reports_page = reports_paginator.get_page(request.GET.get('reports_page'))
    runs = ReconciliationRun.objects.filter(requested_by=request.user)[:25]
    return {'form': form, 'reports_page': reports_page, 'runs': runs}


@login_required
@permission_required('auth.import_mvola_report', raise_exception=True)
def import_report(request):
    form = MvolaReportUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        uploaded = form.cleaned_data['report']
        try:
            store_report(uploaded.name, form.content)
        except ReportAlreadyExists:
            form.add_error('report', 'Un rapport portant ce nom existe déjà; il n’a pas été remplacé.')
        except OSError:
            form.add_error(None, "Le rapport n'a pas pu être enregistré. Vérifiez le dossier de stockage.")
        else:
            messages.success(
                request,
                f'{uploaded.name} importé: {form.row_count} lignes, '
                f'{form.completed_count} transactions Completed.',
            )
            return redirect('mvola:import')

    return render(request, 'mvola/import.html', _import_context(request, form))


@login_required
@permission_required('auth.run_mvola_reconciliation', raise_exception=True)
def launch_reconciliation(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    wants_json = 'application/json' in request.headers.get('Accept', '')

    def launch_error(message, status=400):
        if wants_json:
            return JsonResponse({'ok': False, 'message': message}, status=status)
        messages.error(request, message)
        return redirect('mvola:import')

    filename = request.POST.get('filename', '')
    if (
        '/' in filename
        or '\\' in filename
        or not FILENAME_PATTERN.fullmatch(filename)
    ):
        return launch_error('Nom de rapport invalide.')
    try:
        datetime.strptime(filename[:10], '%Y-%m-%d')
    except ValueError:
        return launch_error('La date du rapport est invalide.')

    input_dir = Path(settings.MVOLA_INPUT_DIR).resolve()
    report_path = (input_dir / filename).resolve()
    if report_path.parent != input_dir or not report_path.is_file():
        return launch_error('Le rapport sélectionné est introuvable.', status=404)

    run = ReconciliationRun.objects.create(
        requested_by=request.user,
        filename=filename,
    )
    run.mark_running()
    try:
        result = reconciliation(filename, request.user.id)
        succeeded = isinstance(result, dict) and result.get('status') == 'success'
        message = (
            result.get('message', 'Rapprochement terminé.')
            if isinstance(result, dict)
            else 'Le service a renvoyé une réponse invalide.'
        )
        process_id = result.get('process_id') if succeeded else None
        if succeeded and process_id is not None:
            try:
                copied_orphans = copy_orphan_processing_history(process_id)
            except Exception:
                logger.exception(
                    'Could not copy orphan rows to processing history for process %s.',
                    process_id,
                )
                message = f'{message} L’historique de régularisation des écarts n’a pas pu être créé.'
            else:
                message = f'{message} {copied_orphans} écart(s) ajouté(s) à l’historique de régularisation.'
    except Exception as exc:
        succeeded = False
        message = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else f"Erreur inattendue pendant le rapprochement ({type(exc).__name__})."
        )
        process_id = None

    final_status = (
        ReconciliationRun.Status.SUCCEEDED
        if succeeded
        else ReconciliationRun.Status.FAILED
    )
    run.mark_finished(status=final_status, message=message, process_id=process_id)

    if wants_json:
        return JsonResponse({
            'ok': succeeded,
            'status': run.status,
            'label': run.get_status_display(),
            'run_id': run.pk,
            'message': run.message,
            'process_id': run.process_id,
            'finished': True,
        })

    if succeeded:
        messages.success(request, message)
    else:
        messages.error(request, message)
    return redirect('mvola:import')


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def reconciliation_history(request):
    form = ReconciliationHistoryFilterForm(request.GET or None)
    processes = []
    if not request.GET or form.is_valid():
        filters = form.cleaned_data if request.GET else {}
        try:
            processes = list_reconciliation_processes(
                date_from=filters.get('date_from'),
                date_to=filters.get('date_to'),
                process_id=filters.get('process_id'),
                status=filters.get('status'),
            )
        except SQLAlchemyError:
            logger.exception('Could not load MVOLA reconciliation history.')
            messages.error(request, "L'historique des rapprochements est temporairement indisponible.")
    return render(request, 'mvola/reconciliation_history.html', {
        'form': form,
        'processes': processes,
    })


def _resolve_detail_source_and_status(request):
    source = request.GET.get('source', 'reconciliation')
    if source not in RESULT_SOURCES:
        source = 'reconciliation'
    valid_statuses = ('orphan_mvola', 'orphan_pamf', 'matched')
    status = request.GET.get('status', 'orphan_mvola') if source == 'reconciliation' else ''
    if source == 'reconciliation' and status not in valid_statuses:
        status = 'orphan_mvola'
    return source, status


def _resolve_detail_filters(request):
    filter_form = ReconciliationDetailFilterForm(request.GET or None)
    filter_values = {}
    filters_valid = True
    if request.GET:
        filters_valid = filter_form.is_valid()
        if filters_valid:
            filter_values = filter_form.cleaned_data
    return filter_form, filter_values, filters_valid


def _latest_orphan_statuses(source, status, rows):
    """Returns {reconciliation_id: raw_status} using the most recent history
    entry per row, defaulting untouched orphan rows to 'orphan_mvola'."""
    latest_statuses = {}
    if source == 'reconciliation' and status == 'orphan_mvola' and rows:
        reconciliation_ids = [row.get('id') for row in rows if row.get('id') is not None]
        latest_statuses = {reconciliation_id: 'orphan_mvola' for reconciliation_id in reconciliation_ids}
        history_entries = (
            OrphanMvolaProcessingHistory.objects
            .filter(InteropMvolaReconciliation_id__in=reconciliation_ids)
            .order_by('-processed_at')
            .values_list('InteropMvolaReconciliation_id', 'status')
        )
        seen = set()
        for reconciliation_id, history_status in history_entries:
            if reconciliation_id in seen:
                continue
            seen.add(reconciliation_id)
            latest_statuses[reconciliation_id] = history_status
    return latest_statuses


ORPHAN_ROW_CSS_CLASS = {
    'orphan_mvola': 'orphan-status-open',
    'processing': 'orphan-status-processing',
}


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def reconciliation_detail(request, process_id):
    try:
        process = get_reconciliation_process(process_id)
    except SQLAlchemyError:
        logger.exception('Could not load MVOLA process %s.', process_id)
        messages.error(request, 'Les détails du rapprochement sont temporairement indisponibles.')
        return redirect('mvola:reconciliation_history')
    if process is None:
        raise Http404('Rapprochement introuvable.')

    source, status = _resolve_detail_source_and_status(request)
    filter_form, filter_values, filters_valid = _resolve_detail_filters(request)
    try:
        page = max(1, int(request.GET.get('page', '1')))
    except ValueError:
        page = 1

    if not filters_valid:
        results = {'columns': RESULT_SOURCES[source]['columns'], 'rows': (), 'total': 0}
    else:
        try:
            results = get_process_result_rows(
                process_id=process_id,
                source=source,
                status=status,
                transaction_id=filter_values.get('transaction_id'),
                phone=filter_values.get('phone'),
                amount=filter_values.get('amount'),
                limit=RESULT_PAGE_SIZE,
                offset=(page - 1) * RESULT_PAGE_SIZE,
            )
        except SQLAlchemyError:
            logger.exception('Could not load details for MVOLA process %s.', process_id)
            messages.error(request, 'Les lignes du rapprochement sont temporairement indisponibles.')
            results = {'columns': RESULT_SOURCES[source]['columns'], 'rows': (), 'total': 0}

    total_pages = max(1, math.ceil(results['total'] / RESULT_PAGE_SIZE))
    page = min(page, total_pages)

    columns = list(results['columns'])
    latest_statuses = _latest_orphan_statuses(source, status, results['rows'])
    if source == 'reconciliation' and status == 'orphan_mvola':
        columns.append(('latest_orphan_status', 'Dernier statut'))

    detail_url = reverse('mvola:reconciliation_detail', args=[process_id])
    source_tabs = [
        {
            'key': 'reconciliation',
            'label': 'Écarts et rapprochement',
            'url': f'{detail_url}?{urlencode({"source": "reconciliation", "status": "orphan_mvola"})}',
        },
        {'key': 'mvola', 'label': 'Transactions MVOLA', 'url': f'{detail_url}?source=mvola'},
        {'key': 'cbs', 'label': 'Transactions CBS', 'url': f'{detail_url}?source=cbs'},
    ]
    filter_params = {
        key: filter_values.get(key) or ''
        for key in ('transaction_id', 'phone', 'amount')
    }
    status_tabs = []
    if source == 'reconciliation':
        status_tabs = [
            {
                'key': tab_status,
                'label': label,
                'url': f'{detail_url}?{urlencode({**filter_params, "source": source, "status": tab_status})}',
            }
            for tab_status, label in (
                ('orphan_mvola', 'Orphan MVOLA'),
                ('orphan_pamf', 'Orphan PAMF'),
                ('matched', 'Rapprochées'),
            )
        ]
    pagination_query = urlencode({**filter_params, 'source': source, 'status': status})
    status_labels = dict(OrphanMvolaProcessingHistory.STATUS_CHOICES)
    is_orphan_mvola_tab = source == 'reconciliation' and status == 'orphan_mvola'
    return render(request, 'mvola/reconciliation_detail.html', {
        'process': process,
        'filter_form': filter_form,
        'source': source,
        'status': status,
        'source_tabs': source_tabs,
        'status_tabs': status_tabs,
        'pagination_query': pagination_query,
        'columns': columns,
        'rows': [
            {
                'id': row.get('id'),
                'row_class': (
                    ORPHAN_ROW_CSS_CLASS.get(latest_statuses.get(row.get('id')), '')
                    if is_orphan_mvola_tab
                    else ''
                ),
                'values': (
                    [row.get(column) for column, _label in results['columns']]
                    + (
                        [status_labels.get(latest_statuses.get(row.get('id')), '-')]
                        if is_orphan_mvola_tab
                        else []
                    )
                ),
            }
            for row in results['rows']
        ],
        'total': results['total'],
        'page': page,
        'total_pages': total_pages,
        'previous_page': page - 1,
        'next_page': page + 1,
        'has_previous': page > 1,
        'has_next': page < total_pages,
    })


SOURCE_EXPORT_LABELS = {
    'reconciliation': 'ecarts_et_rapprochement',
    'mvola': 'transactions_mvola',
    'cbs': 'transactions_cbs',
}


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def reconciliation_detail_export(request, process_id):
    try:
        process = get_reconciliation_process(process_id)
    except SQLAlchemyError:
        logger.exception('Could not load MVOLA process %s.', process_id)
        messages.error(request, 'Les détails du rapprochement sont temporairement indisponibles.')
        return redirect('mvola:reconciliation_history')
    if process is None:
        raise Http404('Rapprochement introuvable.')

    source, status = _resolve_detail_source_and_status(request)
    detail_url = reverse('mvola:reconciliation_detail', args=[process_id])
    back_to_detail = f'{detail_url}?{request.GET.urlencode()}' if request.GET else detail_url

    filter_form, filter_values, filters_valid = _resolve_detail_filters(request)
    if not filters_valid:
        messages.error(request, "Filtres invalides: l'export a été annulé.")
        return redirect(back_to_detail)

    try:
        results = get_process_result_rows(
            process_id=process_id,
            source=source,
            status=status,
            transaction_id=filter_values.get('transaction_id'),
            phone=filter_values.get('phone'),
            amount=filter_values.get('amount'),
            limit=None,
            offset=0,
        )
    except SQLAlchemyError:
        logger.exception('Could not export details for MVOLA process %s.', process_id)
        messages.error(request, "L'export du rapprochement est temporairement indisponible.")
        return redirect(back_to_detail)

    columns = list(results['columns'])
    latest_statuses = _latest_orphan_statuses(source, status, results['rows'])
    if source == 'reconciliation' and status == 'orphan_mvola':
        columns.append(('latest_orphan_status', 'Dernier statut'))

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    filename_bits = ['rapprochement', str(process_id), SOURCE_EXPORT_LABELS.get(source, source)]
    if status:
        filename_bits.append(status)
    response['Content-Disposition'] = f'attachment; filename="{"_".join(filename_bits)}.csv"'

    response.write('﻿')  # BOM so Excel opens accented characters correctly.
    writer = csv.writer(response, delimiter=';')
    writer.writerow([label for _key, label in columns])
    for row in results['rows']:
        values = [row.get(column) for column, _label in results['columns']]
        if source == 'reconciliation' and status == 'orphan_mvola':
            values.append(latest_statuses.get(row.get('id'), ''))
        writer.writerow(['' if value is None else value for value in values])
    return response


def _load_cbs_transaction(row):
    if not row.transid_mvola:
        return None, False
    try:
        return get_transaction_by_requestID(row.transid_mvola), False
    except Exception:
        logger.exception(
            'Could not fetch CBS transaction details for TRANSID_MVOLA %s.',
            row.transid_mvola,
        )
        return None, True


def _get_orphan_mvola_row(reconciliation_id):
    row = get_object_or_404(InteropMvolaReconciliation, pk=reconciliation_id)
    if row.reconciliation_status != 'orphan_mvola':
        raise Http404('Cette transaction n’est pas une orpheline MVOLA.')
    return row


def _orphan_detail_context(request, row, action_form=None):
    cbs_transaction, cbs_lookup_failed = _load_cbs_transaction(row)
    history = OrphanMvolaProcessingHistory.objects.filter(
        InteropMvolaReconciliation_id=row.pk,
    ).order_by('-processed_at')
    return {
        'row': row,
        'cbs_transaction': cbs_transaction,
        'cbs_lookup_failed': cbs_lookup_failed,
        'history': history,
        'can_manage_orphans': request.user.has_perm('auth.run_mvola_reconciliation'),
        'action_form': action_form if action_form is not None else OrphanActionForm(),
    }


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def orphan_transaction_detail(request, reconciliation_id):
    row = _get_orphan_mvola_row(reconciliation_id)
    return render(
        request,
        'mvola/partials/orphan_detail_modal.html',
        _orphan_detail_context(request, row),
    )


@login_required
@permission_required('auth.run_mvola_reconciliation', raise_exception=True)
def orphan_transaction_action(request, reconciliation_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    row = _get_orphan_mvola_row(reconciliation_id)
    form = OrphanActionForm(request.POST)
    if form.is_valid():
        is_retried = OrphanMvolaProcessingHistory.objects.filter(
            InteropMvolaReconciliation_id=row.pk,
            status__in=('processing', 'done'),
        ).exists()
        OrphanMvolaProcessingHistory.objects.create(
            InteropMvolaReconciliation=row,
            status=form.cleaned_data['status'],
            comments=form.cleaned_data['comments'],
            is_retried=is_retried,
        )
        form = OrphanActionForm()

    return render(
        request,
        'mvola/partials/orphan_detail_modal.html',
        _orphan_detail_context(request, row, action_form=form),
    )


def _get_orphan_pamf_row(reconciliation_id):
    row = get_object_or_404(InteropMvolaReconciliation, pk=reconciliation_id)
    if row.reconciliation_status != 'orphan_pamf':
        raise Http404('Cette transaction n’est pas une orpheline PAMF.')
    return row


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def orphan_pamf_detail(request, reconciliation_id):
    row = _get_orphan_pamf_row(reconciliation_id)
    cbs_transactions = (
        InteropMvolaCbs.objects.filter(trx_id=row.trx_id).order_by('-id')
        if row.trx_id else InteropMvolaCbs.objects.none()
    )
    mvola_transactions = (
        InteropMvolaMvola.objects.filter(transid_mvola=row.trx_id).order_by('-id')
        if row.trx_id else InteropMvolaMvola.objects.none()
    )
    return render(request, 'mvola/partials/orphan_pamf_detail_modal.html', {
        'row': row,
        'cbs_transactions': cbs_transactions,
        'mvola_transactions': mvola_transactions,
    })