"""Requêtes de rapport multi-processus sur les tables externes interop_*.

Lecture seule, SQL brut via le moteur PostgreSQL partagé (comme results_repository).
La configuration par module décrit uniquement les tables/colonnes: le SQL est construit
à partir de fragments constants, les valeurs de filtres passent toujours en paramètres.
"""
from sqlalchemy import text

AMOUNT_GUARD = r"'^[0-9]+(\.[0-9]+)?$'"

# Régularisation: dernier état de l'historique, 'open' tant qu'aucune action n'a été enregistrée.
REGULARIZATION_SQL = '''CASE
    WHEN r.reconciliation_status = 'matched' THEN NULL
    WHEN h.status IN ('processing', 'done') THEN h.status
    ELSE 'open'
END'''

MODULES = {
    'mvola': {
        'label': 'WTB MVOLA',
        'table': 'interop_mvola_reconciliation',
        'process_table': 'interop_mvola_process',
        'history_table': 'mvola_orphanmvolaprocessinghistory',
        'history_fk': 'InteropMvolaReconciliation_id',
        'statuses': (
            ('matched', 'Rapprochées'),
            ('orphan_mvola', 'Orphelines MVOLA'),
            ('orphan_pamf', 'Orphelines PAMF'),
        ),
        'phone_label': 'Téléphone',
        # Montant effectif: côté MVOLA s'il existe, sinon côté PAMF.
        'amount_expr': 'COALESCE(r."AMOUNT"::numeric, r."AmountCRY"::numeric)',
        'phone_clause': "COALESCE(r.\"MSISDN\"::text, '') ILIKE :phone",
        'transaction_clause': '''(
            COALESCE(r."TRANSID_MVOLA"::text, '') ILIKE :transaction_id
            OR COALESCE(r.trx_id::text, '') ILIKE :transaction_id
        )''',
        'columns': (
            ('process_id', 'Processus', 'r.process_id'),
            ('transaction_date', 'Date du rapport', 'p.transaction_date'),
            ('reconciliation_status', 'Statut', 'r.reconciliation_status'),
            ('regularization', 'Régularisation', REGULARIZATION_SQL),
            ('TRANSID_MVOLA', 'Transaction MVOLA', 'r."TRANSID_MVOLA"'),
            ('trx_id', 'Transaction PAMF', 'r.trx_id'),
            ('DATE_TRANS', 'Date MVOLA', 'r."DATE_TRANS"'),
            ('PostingDate', 'Date PAMF', 'r."PostingDate"'),
            ('MSISDN', 'Téléphone', 'r."MSISDN"'),
            ('NOM', 'Nom', 'r."NOM"'),
            ('TYPE_OPERATION', 'Opération', 'r."TYPE_OPERATION"'),
            ('AMOUNT', 'Montant MVOLA', 'r."AMOUNT"'),
            ('AmountCRY', 'Montant PAMF', 'r."AmountCRY"'),
        ),
    },
    'om': {
        'label': 'WTB Orange Money',
        'table': 'interop_om_reconciliation',
        'process_table': 'interop_om_process',
        'history_table': 'om_orphanomprocessinghistory',
        'history_fk': 'InteropOmReconciliation_id',
        'statuses': (
            ('matched', 'Rapprochées'),
            ('orphan_om', 'Orphelines Orange Money'),
            ('orphan_pamf', 'Orphelines PAMF'),
        ),
        'phone_label': 'Téléphone / compte / pseudo',
        'amount_expr': f'''(CASE
            WHEN r.debit ~ {AMOUNT_GUARD} AND r.debit::numeric > 0 THEN r.debit::numeric
            WHEN r.credit ~ {AMOUNT_GUARD} THEN r.credit::numeric
            ELSE r."AmountCRY"::numeric
        END)''',
        'phone_clause': '''(
            COALESCE(r.client_account::text, '') ILIKE :phone
            OR COALESCE(r.pseudo::text, '') ILIKE :phone
        )''',
        'transaction_clause': '''(
            COALESCE(r.trx_id::text, '') ILIKE :transaction_id
            OR COALESCE(r."apiLogId"::text, '') ILIKE :transaction_id
        )''',
        'columns': (
            ('process_id', 'Processus', 'r.process_id'),
            ('transaction_date', 'Date du rapport', 'p.transaction_date'),
            ('reconciliation_status', 'Statut', 'r.reconciliation_status'),
            ('regularization', 'Régularisation', REGULARIZATION_SQL),
            ('trx_id', 'Transaction OM / CBS', 'r.trx_id'),
            ('date', 'Date OM', 'r."date"'),
            ('hour', 'Heure OM', 'r."hour"'),
            ('PostingDate', 'Date CBS', 'r."PostingDate"'),
            ('pseudo', 'Pseudo', 'r.pseudo'),
            ('client_account', 'Compte client', 'r.client_account'),
            ('transaction_type', 'Type de transaction', 'r.transaction_type'),
            ('debit', 'Débit OM', 'r.debit'),
            ('credit', 'Crédit OM', 'r.credit'),
            ('AmountCRY', 'Montant CBS', 'r."AmountCRY"'),
        ),
    },
}

REGULARIZATION_CHOICES = (
    ('open', 'Ouverte'),
    ('processing', 'En cours'),
    ('done', 'Régularisée'),
)


def get_engine(module):
    # Import tardif: chaque service possède sa propre fabrique de moteur.
    if module == 'om':
        from service_om import get_pg_engine
    else:
        from service_mvola import get_pg_engine
    return get_pg_engine()


def report_columns(module):
    return tuple((key, label) for key, label, _sql in MODULES[module]['columns'])


def report_rows(
    module, *, date_from=None, date_to=None, process_id=None, status=None,
    regularization=None, transaction_id=None, phone=None, amount=None,
    amount_min=None, amount_max=None, limit=50, offset=0,
):
    """Une page de lignes (ou toutes si limit est None, pour l'export) + le total."""
    config = MODULES[module]
    clauses, params = [], {}
    if date_from:
        clauses.append('p.transaction_date >= :date_from')
        params['date_from'] = date_from
    if date_to:
        clauses.append('p.transaction_date <= :date_to')
        params['date_to'] = date_to
    if process_id:
        clauses.append('r.process_id = :process_id')
        params['process_id'] = process_id
    if status:
        clauses.append('r.reconciliation_status = :status')
        params['status'] = status
    if regularization:
        clauses.append(f'({REGULARIZATION_SQL}) = :regularization')
        params['regularization'] = regularization
    if transaction_id:
        clauses.append(config['transaction_clause'])
        params['transaction_id'] = f'%{transaction_id.strip()}%'
    if phone:
        clauses.append(config['phone_clause'])
        params['phone'] = f'%{phone.strip()}%'
    amount_expr = config['amount_expr']
    if amount is not None:
        clauses.append(f'{amount_expr} = :amount')
        params['amount'] = amount
    if amount_min is not None:
        clauses.append(f'{amount_expr} >= :amount_min')
        params['amount_min'] = amount_min
    if amount_max is not None:
        clauses.append(f'{amount_expr} <= :amount_max')
        params['amount_max'] = amount_max
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ''

    from_clause = f'''
        FROM public.{config["table"]} r
        JOIN public.{config["process_table"]} p ON p.id = r.process_id
        LEFT JOIN LATERAL (
            SELECT oh.status
            FROM public.{config["history_table"]} oh
            WHERE oh."{config["history_fk"]}" = r.id
            ORDER BY oh.processed_at DESC, oh.id DESC
            LIMIT 1
        ) h ON true
        {where_clause}
    '''
    selected = ', '.join(f'{sql} AS "{key}"' for key, _label, sql in config['columns'])
    limit_clause = ''
    row_params = dict(params)
    if limit is not None:
        limit_clause = 'LIMIT :limit OFFSET :offset'
        row_params.update(limit=limit, offset=offset)
    rows_query = text(f'''
        SELECT {selected} {from_clause}
        ORDER BY p.transaction_date DESC, r.process_id DESC, r.id
        {limit_clause}
    ''')
    count_query = text(f'SELECT COUNT(*) {from_clause}')
    with get_engine(module).connect() as connection:
        total = connection.execute(count_query, params).scalar_one()
        rows = [dict(row) for row in connection.execute(rows_query, row_params).mappings()]
    return {'columns': report_columns(module), 'rows': rows, 'total': total}
