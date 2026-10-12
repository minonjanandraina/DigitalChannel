from django.contrib.auth.decorators import login_required, permission_required
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render

from mvola.models import InteropMvolaReconciliation, OrphanMvolaProcessingHistory
from om.models import InteropOmReconciliation, OrphanOmProcessingHistory

from . import service
from .models import ReplayRequest

# Par source: modèle de réconciliation, statut orphelin, historique de régularisation
# et extraction du RequestId / montant de la ligne.
SOURCES = {
    ReplayRequest.Source.MVOLA: {
        'model': InteropMvolaReconciliation,
        'orphan_status': 'orphan_mvola',
        'history_model': OrphanMvolaProcessingHistory,
        'history_fk': 'InteropMvolaReconciliation',
        'request_id': lambda row: row.transid_mvola,
        'amount': lambda row: service.parse_amount(row.amount),
    },
    ReplayRequest.Source.OM: {
        'model': InteropOmReconciliation,
        'orphan_status': 'orphan_om',
        'history_model': OrphanOmProcessingHistory,
        'history_fk': 'InteropOmReconciliation',
        'request_id': lambda row: row.trx_id,
        'amount': lambda row: service.parse_amount(row.debit, row.credit),
    },
}


def _get_row(source, reconciliation_id):
    config = SOURCES.get(source)
    if config is None:
        raise Http404('Source inconnue.')
    row = get_object_or_404(config['model'], pk=reconciliation_id)
    if row.reconciliation_status != config['orphan_status']:
        raise Http404('Cette transaction n’est pas une orpheline rejouable.')
    return config, row


def _record_history(config, row, *, status, comments, response_message=''):
    history_model = config['history_model']
    fk = config['history_fk']
    is_retried = history_model.objects.filter(
        **{f'{fk}_id': row.pk}, status__in=('processing', 'done'),
    ).exists()
    history_model.objects.create(**{
        fk: row, 'status': status, 'comments': comments,
        'response_message': response_message, 'is_retried': is_retried,
    })


def _panel(request, source, row, *, message=None, success=True):
    config = SOURCES[source]
    request_id = config['request_id'](row)
    replays = ReplayRequest.objects.filter(
        source=source, reconciliation_id=row.pk,
    ).select_related('maker', 'checker').prefetch_related('logs__user')
    latest = replays.first()
    user = request.user
    return render(request, 'replay/panel.html', {
        'source': source,
        'row': row,
        'request_id': request_id,
        'amount': config['amount'](row),
        'replays': replays,
        'latest': latest,
        'can_make': (
            user.has_perm('auth.replay_transaction_maker')
            and (latest is None or latest.status == ReplayRequest.Status.MAKER_FAILED)
        ),
        'can_check': (
            user.has_perm('auth.replay_transaction_checker')
            and latest is not None
            and latest.status in (ReplayRequest.Status.PENDING_CHECKER, ReplayRequest.Status.CHECKER_FAILED)
            and not replays.filter(request_id=latest.request_id, maker=user).exists()
        ),
        'message': message,
        'success': success,
    })


@login_required
@permission_required('auth.view_reconciliation_results', raise_exception=True)
def replay_panel(request, source, reconciliation_id):
    _, row = _get_row(source, reconciliation_id)
    return _panel(request, source, row)


@login_required
@permission_required('auth.replay_transaction_maker', raise_exception=True)
def replay_maker(request, source, reconciliation_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    config, row = _get_row(source, reconciliation_id)
    try:
        replay, success, message = service.submit_maker(
            source=source,
            reconciliation_id=row.pk,
            request_id=config['request_id'](row),
            amount=config['amount'](row),
            user=request.user,
        )
    except service.ReplayError as exc:
        return _panel(request, source, row, message=str(exc), success=False)
    if success:
        _record_history(
            config, row, status='processing',
            comments=f'Rejeu demandé par {request.user.email} (maker), en attente du checker.',
        )
    return _panel(request, source, row, message=message, success=success)


@login_required
@permission_required('auth.replay_transaction_checker', raise_exception=True)
def replay_checker(request, source, reconciliation_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    config, row = _get_row(source, reconciliation_id)
    latest = ReplayRequest.objects.filter(source=source, reconciliation_id=row.pk).first()
    if latest is None:
        raise Http404('Aucune demande de rejeu pour cette transaction.')
    try:
        replay, success, message = service.submit_checker(replay_id=latest.pk, user=request.user)
    except service.ReplayError as exc:
        return _panel(request, source, row, message=str(exc), success=False)
    if success:
        _record_history(
            config, row, status='done',
            comments=f'Rejeu validé par {request.user.email} (checker); demandé par {replay.maker.email}.',
        )
    return _panel(request, source, row, message=message, success=success)
