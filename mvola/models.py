from django.conf import settings
from django.db import models
from django.utils import timezone


class InteropMvolaProcess(models.Model):
    id = models.BigAutoField(primary_key=True)
    user_id = models.BigIntegerField(db_column='userID')
    status = models.TextField(db_column='Status')
    insert_date = models.DateTimeField(db_column='insertDate')
    transaction_date = models.DateField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'interop_mvola_process'

    def __str__(self):
        return f'Processus #{self.pk} ({self.status})'


class InteropMvolaCbs(models.Model):
    id = models.BigAutoField(primary_key=True)
    process = models.ForeignKey(
        InteropMvolaProcess,
        on_delete=models.DO_NOTHING,
        db_column='process_id',
        related_name='cbs_transactions',
    )
    api_log_id = models.TextField(db_column='apiLogId', null=True, blank=True)
    posting_date = models.TextField(db_column='PostingDate', null=True, blank=True)
    amount_cry = models.FloatField(db_column='AmountCRY', null=True, blank=True)
    trx_id = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'interop_mvola_cbs'


class InteropMvolaMvola(models.Model):
    id = models.BigAutoField(primary_key=True)
    process = models.ForeignKey(
        InteropMvolaProcess,
        on_delete=models.DO_NOTHING,
        db_column='process_id',
        related_name='mvola_transactions',
    )
    date_trans = models.TextField(db_column='DATE_TRANS', null=True, blank=True)
    transid_mvola = models.TextField(db_column='TRANSID_MVOLA', null=True, blank=True)
    state = models.TextField(db_column='STATE', null=True, blank=True)
    msisdn = models.TextField(db_column='MSISDN', null=True, blank=True)
    pivot = models.BigIntegerField(db_column='PIVOT', null=True, blank=True)
    sens = models.TextField(db_column='SENS', null=True, blank=True)
    nom = models.TextField(db_column='NOM', null=True, blank=True)
    transid_parent = models.BigIntegerField(db_column='TRANSID_PARENT', null=True, blank=True)
    trans_type = models.TextField(db_column='TRANS_TYPE', null=True, blank=True)
    amount = models.BigIntegerField(db_column='AMOUNT', null=True, blank=True)
    solde_pivot_avant = models.BigIntegerField(db_column='SOLDE_PIVOT_AVANT', null=True, blank=True)
    solde_pivot_apres = models.BigIntegerField(db_column='SOLDE_PIVOT_APRES', null=True, blank=True)
    origftid = models.TextField(db_column='ORIGFTID', null=True, blank=True)
    type_operation = models.TextField(db_column='TYPE_OPERATION', null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'interop_mvola_mvola'


class InteropMvolaReconciliation(models.Model):
    id = models.BigAutoField(primary_key=True)
    process = models.ForeignKey(
        InteropMvolaProcess,
        on_delete=models.DO_NOTHING,
        db_column='process_id',
        related_name='reconciliation_rows',
    )
    date_trans = models.TextField(db_column='DATE_TRANS', null=True, blank=True)
    transid_mvola = models.TextField(db_column='TRANSID_MVOLA', null=True, blank=True)
    state = models.TextField(db_column='STATE', null=True, blank=True)
    msisdn = models.TextField(db_column='MSISDN', null=True, blank=True)
    pivot = models.BigIntegerField(db_column='PIVOT', null=True, blank=True)
    sens = models.TextField(db_column='SENS', null=True, blank=True)
    nom = models.TextField(db_column='NOM', null=True, blank=True)
    transid_parent = models.BigIntegerField(db_column='TRANSID_PARENT', null=True, blank=True)
    trans_type = models.TextField(db_column='TRANS_TYPE', null=True, blank=True)
    amount = models.BigIntegerField(db_column='AMOUNT', null=True, blank=True)
    solde_pivot_avant = models.BigIntegerField(db_column='SOLDE_PIVOT_AVANT', null=True, blank=True)
    solde_pivot_apres = models.BigIntegerField(db_column='SOLDE_PIVOT_APRES', null=True, blank=True)
    origftid = models.TextField(db_column='ORIGFTID', null=True, blank=True)
    type_operation = models.TextField(db_column='TYPE_OPERATION', null=True, blank=True)
    api_log_id = models.TextField(db_column='apiLogId', null=True, blank=True)
    posting_date = models.TextField(db_column='PostingDate', null=True, blank=True)
    amount_cry = models.FloatField(db_column='AmountCRY', null=True, blank=True)
    trx_id = models.TextField(null=True, blank=True)
    reconciliation_status = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'interop_mvola_reconciliation'


class ReconciliationRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'En attente'
        RUNNING = 'running', 'En cours'
        SUCCEEDED = 'succeeded', 'Terminé'
        FAILED = 'failed', 'Échec'

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='mvola_reconciliation_runs',
    )
    filename = models.CharField(max_length=255)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    queue_task_id = models.CharField(max_length=32, blank=True)
    process_id = models.PositiveBigIntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        permissions = [
            ('view_mvola_reconciliation_runs', 'Can view MVOLA reconciliation runs'),
        ]

    def mark_running(self):
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=('status', 'started_at'))

    def mark_finished(self, *, status, message, process_id=None):
        self.status = status
        self.message = message
        self.process_id = process_id
        self.finished_at = timezone.now()
        self.save(update_fields=('status', 'message', 'process_id', 'finished_at'))

    def __str__(self):
        return f'{self.filename} ({self.get_status_display()})'
    
class OrphanMvolaProcessingHistory(models.Model):
    STATUS_CHOICES = [
        ('orphan_mvola','Orphan mvola'),
        ('orphan_pamf','Orphan pamf'),
        ('processing', 'Processing'),
        ('done', 'Done'),
    ]
    InteropMvolaReconciliation = models.ForeignKey(
        InteropMvolaReconciliation,
        on_delete=models.CASCADE,
        related_name='orphan_processing_history',
    )
    processed_at = models.DateTimeField(auto_now_add=True)
    is_retried = models.BooleanField(default=False)
    response_message = models.TextField(blank=True)
    status= models.CharField(max_length=16,blank=True, choices=STATUS_CHOICES)
    comments = models.TextField(blank=True)