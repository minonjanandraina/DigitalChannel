from django.conf import settings
from django.db import models


class ReplayRequest(models.Model):
    """Une demande de rejeu d'une transaction orpheline (maker puis checker)."""

    class Source(models.TextChoices):
        MVOLA = 'mvola', 'MVOLA'
        OM = 'om', 'Orange Money'

    class Status(models.TextChoices):
        PENDING_CHECKER = 'pending_checker', 'En attente du checker'
        MAKER_FAILED = 'maker_failed', 'Échec maker'
        CHECKER_FAILED = 'checker_failed', 'Échec checker'
        COMPLETED = 'completed', 'Rejouée'

    # MerchantType attendu par l'endpoint.
    MERCHANT_TYPES = {Source.MVOLA: '13', Source.OM: '9'}

    source = models.CharField(max_length=8, choices=Source.choices)
    reconciliation_id = models.BigIntegerField()
    request_id = models.CharField(max_length=100, db_index=True)
    amount = models.BigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, db_index=True)
    maker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='replay_requests_made',
    )
    checker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='replay_requests_checked',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [models.Index(fields=('source', 'reconciliation_id'))]

    @property
    def merchant_type(self):
        return self.MERCHANT_TYPES[self.source]

    def __str__(self):
        return f'{self.source}:{self.request_id} ({self.get_status_display()})'


class ReplayLog(models.Model):
    """Historique de chaque appel envoyé à l'endpoint (la clé API n'est jamais stockée)."""

    class Role(models.TextChoices):
        MAKER = 'maker', 'Maker'
        CHECKER = 'checker', 'Checker'

    request = models.ForeignKey(ReplayRequest, on_delete=models.CASCADE, related_name='logs')
    role = models.CharField(max_length=8, choices=Role.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='replay_logs',
    )
    user_mail = models.EmailField()
    url = models.CharField(max_length=255)
    request_body = models.TextField()
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    success = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-sent_at', '-id')
