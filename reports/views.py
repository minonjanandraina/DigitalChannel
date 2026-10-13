import csv
import logging
import math
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from sqlalchemy.exc import SQLAlchemyError

from .forms import ReportFilterForm
from .repository import MODULES, REGULARIZATION_CHOICES, report_columns, report_rows

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
FILTER_KEYS = tuple(ReportFilterForm.base_fields)
REGULARIZATION_LABELS = dict(REGULARIZATION_CHOICES)


def _display(module, key, value):
    if value is None:
        return ''
    if key == 'regularization':
        return REGULARIZATION_LABELS.get(value, value)
    if key == 'reconciliation_status':
        return dict(MODULES[module]['statuses']).get(value, value)
    return value


def _filters(request, module):
    form = ReportFilterForm(request.GET or None, module=module)
    values = {}
    valid = True
    if request.GET:
        valid = form.is_valid()
        if valid:
            values = {key: form.cleaned_data.get(key) for key in FILTER_KEYS}
            values = {key: value for key, value in values.items() if value not in (None, '')}
    return form, values, valid


def _module_or_404(module):
    if module not in MODULES:
        raise Http404('Module inconnu.')
    return MODULES[module]


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def report(request, module):
    config = _module_or_404(module)
    form, filters, valid = _filters(request, module)
    try:
        page = max(1, int(request.GET.get('page', '1')))
    except ValueError:
        page = 1

    columns = report_columns(module)
    results = {'columns': columns, 'rows': [], 'total': 0}
    searched = bool(request.GET) and valid
    if searched:
        try:
            results = report_rows(
                module, **filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
            )
        except SQLAlchemyError:
            logger.exception('Could not build %s report.', module)
            messages.error(request, 'Le rapport est temporairement indisponible.')

    total_pages = max(1, math.ceil(results['total'] / PAGE_SIZE))
    page = min(page, total_pages)
    query = request.GET.copy()
    query.pop('page', None)
    return render(request, 'reports/report.html', {
        'module': module,
        'module_label': config['label'],
        'form': form,
        'searched': searched,
        'columns': columns,
        'rows': [
            [_display(module, key, row.get(key)) for key, _label in columns]
            for row in results['rows']
        ],
        'total': results['total'],
        'page': page,
        'total_pages': total_pages,
        'has_previous': page > 1,
        'has_next': page < total_pages,
        'previous_page': page - 1,
        'next_page': page + 1,
        'query': query.urlencode(),
    })


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def report_export(request, module):
    _module_or_404(module)
    form, filters, valid = _filters(request, module)
    back = reverse('reports:report', args=[module])
    if not valid:
        messages.error(request, "Filtres invalides: l'export a été annulé.")
        return redirect(f'{back}?{request.GET.urlencode()}')
    try:
        results = report_rows(module, **filters, limit=None)
    except SQLAlchemyError:
        logger.exception('Could not export %s report.', module)
        messages.error(request, "L'export du rapport est temporairement indisponible.")
        return redirect(f'{back}?{request.GET.urlencode()}')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="rapport_{module}_{date.today():%Y%m%d}.csv"'
    response.write('﻿')  # BOM pour qu'Excel lise correctement les accents.
    writer = csv.writer(response, delimiter=';')
    writer.writerow([label for _key, label in results['columns']])
    for row in results['rows']:
        writer.writerow([_display(module, key, row.get(key)) for key, _label in results['columns']])
    return response
