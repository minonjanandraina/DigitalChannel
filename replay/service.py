"""Rejeu de transactions orphelines via l'endpoint RejoutTransaction (maker/checker).

Le maker soumet la demande; l'endpoint ne traite la transaction (identifiée par
RequestId) qu'après validation du checker. Chaque envoi est historisé dans ReplayLog.
"""
import json
import logging
import socket
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ReplayLog, ReplayRequest

logger = logging.getLogger(__name__)

MAX_STORED_RESPONSE = 5000


class ReplayError(Exception):
    """Erreur métier affichable à l'utilisateur."""


def parse_amount(*candidates):
    """Premier montant strictement positif parmi les candidats, en entier."""
    for candidate in candidates:
        if candidate in (None, ''):
            continue
        try:
            value = Decimal(str(candidate).replace(' ', '').replace(',', '.'))
        except InvalidOperation:
            continue
        if value > 0:
            return int(value)
    return None


def _post(role, path, payload):
    base_url = (settings.REPLAY_API_BASE_URL or '').rstrip('/')
    api_key = settings.REPLAY_API_KEY
    if not base_url or not api_key:
        raise ReplayError('Le rejeu de transaction n’est pas configuré (REPLAY_API_BASE_URL / REPLAY_API_KEY).')
    url = f'{base_url}/{path}'
    http_request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'x-api-key': api_key},
        method='POST',
    )
    http_status, body, error = None, '', ''
    try:
        with urllib.request.urlopen(http_request, timeout=settings.REPLAY_API_TIMEOUT) as response:
            http_status = response.status
            body = response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        body = exc.read().decode('utf-8', errors='replace')
        error = f'HTTP {exc.code}'
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        logger.exception('Replay %s call failed.', role)
        error = f'Endpoint injoignable: {exc}'
    success = http_status is not None and 200 <= http_status < 300
    return url, http_status, body[:MAX_STORED_RESPONSE], success, error


def _send(replay, role, user, path, payload):
    url, http_status, body, success, error = _post(role, path, payload)
    ReplayLog.objects.create(
        request=replay, role=role, user=user, user_mail=user.email, url=url,
        request_body=json.dumps(payload), http_status=http_status,
        response_body=body, success=success, error=error,
    )
    return success, http_status, body, error


def _failure_message(role_label, http_status, body, error):
    detail = error or f'HTTP {http_status}'
    if body:
        detail = f'{detail} — {body[:300]}'
    return f'{role_label}: échec de l’envoi ({detail}).'


def submit_maker(*, source, reconciliation_id, request_id, amount, user):
    """Crée la demande et l'envoie à l'endpoint maker. Retourne (replay, success, message)."""
    if not user.email:
        raise ReplayError('Votre compte n’a pas d’adresse email (UserMail requis).')
    if not request_id or not amount:
        raise ReplayError('RequestId ou montant manquant pour cette transaction.')

    with transaction.atomic():
        already_active = ReplayRequest.objects.select_for_update().filter(
            source=source, request_id=request_id,
            status__in=(
                ReplayRequest.Status.PENDING_CHECKER,
                ReplayRequest.Status.CHECKER_FAILED,
                ReplayRequest.Status.COMPLETED,
            ),
        ).exists()
        if already_active:
            raise ReplayError('Une demande de rejeu existe déjà pour cette transaction.')
        replay = ReplayRequest.objects.create(
            source=source, reconciliation_id=reconciliation_id, request_id=request_id,
            amount=amount, status=ReplayRequest.Status.MAKER_FAILED, maker=user,
        )
        payload = {
            'RequestId': request_id,
            'MerchantType': replay.merchant_type,
            'Amount': amount,
            'UserMail': user.email,
        }
        success, http_status, body, error = _send(replay, ReplayLog.Role.MAKER, user, 'maker', payload)
        if success:
            replay.status = ReplayRequest.Status.PENDING_CHECKER
            replay.save(update_fields=('status',))
            return replay, True, 'Demande de rejeu envoyée, en attente de validation par un checker.'
    return replay, False, _failure_message('Maker', http_status, body, error)


def submit_checker(*, replay_id, user):
    """Valide la demande auprès de l'endpoint checker. Retourne (replay, success, message)."""
    if not user.email:
        raise ReplayError('Votre compte n’a pas d’adresse email (UserMail requis).')
    with transaction.atomic():
        replay = ReplayRequest.objects.select_for_update().get(pk=replay_id)
        if replay.status not in (ReplayRequest.Status.PENDING_CHECKER, ReplayRequest.Status.CHECKER_FAILED):
            raise ReplayError('Cette demande n’est pas en attente de validation.')
        if ReplayRequest.objects.filter(
            source=replay.source, request_id=replay.request_id, maker=user,
        ).exists():
            raise ReplayError('Un utilisateur ayant été maker sur cette transaction ne peut pas être checker.')
        payload = {'RequestId': replay.request_id, 'UserMail': user.email}
        success, http_status, body, error = _send(replay, ReplayLog.Role.CHECKER, user, 'checker', payload)
        replay.checker = user
        replay.checked_at = timezone.now()
        replay.status = ReplayRequest.Status.COMPLETED if success else ReplayRequest.Status.CHECKER_FAILED
        replay.save(update_fields=('checker', 'checked_at', 'status'))
    if success:
        return replay, True, 'Transaction validée et rejouée par l’endpoint.'
    return replay, False, _failure_message('Checker', http_status, body, error)
