from django.urls import path

from . import views

app_name = 'mvola'

urlpatterns = [
    path('import/', views.import_report, name='import'),
    path('reconcile/', views.launch_reconciliation, name='launch_reconciliation'),
    path('reconciliations/', views.reconciliation_history, name='reconciliation_history'),
    path('reconciliations/<int:process_id>/', views.reconciliation_detail, name='reconciliation_detail'),
    path(
        'reconciliations/<int:process_id>/export/',
        views.reconciliation_detail_export,
        name='reconciliation_detail_export',
    ),
    path(
        'reconciliations/orphans/<int:reconciliation_id>/',
        views.orphan_transaction_detail,
        name='orphan_detail',
    ),
    path(
        'reconciliations/orphans/<int:reconciliation_id>/actions/',
        views.orphan_transaction_action,
        name='orphan_action',
    ),
    path(
        'reconciliations/orphans-pamf/<int:reconciliation_id>/',
        views.orphan_pamf_detail,
        name='orphan_pamf_detail',
    ),
    path(
        'reconciliations/matched/<int:reconciliation_id>/',
        views.matched_transaction_detail,
        name='matched_detail',
    ),
]