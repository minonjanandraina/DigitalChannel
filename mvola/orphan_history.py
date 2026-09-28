from django.db import transaction

from .models import InteropMvolaReconciliation, OrphanMvolaProcessingHistory

ORPHAN_STATUSES = ('orphan_mvola', 'orphan_pamf')


def copy_orphan_processing_history(process_id):
    with transaction.atomic():
        orphan_rows = list(
            InteropMvolaReconciliation.objects.select_for_update()
            .filter(
                process_id=process_id,
                reconciliation_status__in=ORPHAN_STATUSES,
            )
            .only('id', 'reconciliation_status')
        )
        if not orphan_rows:
            return 0

        reconciliation_ids = [row.pk for row in orphan_rows]
        existing_ids = set(
            OrphanMvolaProcessingHistory.objects.filter(
                InteropMvolaReconciliation_id__in=reconciliation_ids,
            ).values_list('InteropMvolaReconciliation_id', flat=True)
        )
        new_history = [
            OrphanMvolaProcessingHistory(
                InteropMvolaReconciliation=row,
                status=row.reconciliation_status,
            )
            for row in orphan_rows
            if row.pk not in existing_ids
        ]
        if new_history:
            OrphanMvolaProcessingHistory.objects.bulk_create(new_history)
        return len(new_history)
