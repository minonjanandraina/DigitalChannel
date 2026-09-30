from sqlalchemy import text

from service_om import get_pg_engine

AMOUNT_GUARD = r"'^[0-9]+(\.[0-9]+)?$'"

RESULT_SOURCES = {
    'reconciliation': {
        'table': 'interop_om_reconciliation',
        'columns': (
            ('trx_id', 'Transaction OM / CBS'),
            ('reconciliation_status', 'Statut'),
            ('date', 'Date OM'),
            ('PostingDate', 'Date CBS'),
            ('pseudo', 'Pseudo'),
            ('amounts', 'Montant (débit/crédit OM / CBS)'),
        ),
        'select': (
            ('id', 'id'),
            ('trx_id', 'trx_id'),
            ('"reconciliation_status"', 'reconciliation_status'),
            ('"date"', 'date'),
            ('"PostingDate"', 'PostingDate'),
            ('pseudo', 'pseudo'),
            (
                '''concat_ws(' / ',
                    NULLIF(debit, ''), NULLIF(credit, ''), NULLIF("AmountCRY"::text, '')
                )''',
                'amounts',
            ),
        ),
        'filters': {
            'transaction_id': '''(
                COALESCE(trx_id::text, '') ILIKE :transaction_id
                OR COALESCE("apiLogId"::text, '') ILIKE :transaction_id
            )''',
            'account': "COALESCE(pseudo::text, '') ILIKE :account",
            'amount': f'''(
                (debit ~ {AMOUNT_GUARD} AND debit::numeric = :amount)
                OR (credit ~ {AMOUNT_GUARD} AND credit::numeric = :amount)
                OR "AmountCRY" = :amount
            )''',
        },
    },
    'om': {
        'table': 'interop_om_om',
        'columns': (
            ('trx_id', 'Transaction OM'),
            ('date', 'Date'),
            ('hour', 'Heure'),
            ('status', 'Statut OM'),
            ('pseudo', 'Pseudo'),
            ('debit', 'Débit'),
            ('credit', 'Crédit'),
        ),
        'select': tuple((f'"{column}"', column) for column, _label in (
            ('trx_id', 'Transaction OM'),
            ('date', 'Date'),
            ('hour', 'Heure'),
            ('status', 'Statut OM'),
            ('pseudo', 'Pseudo'),
            ('debit', 'Débit'),
            ('credit', 'Crédit'),
        )),
        'filters': {
            'transaction_id': "COALESCE(trx_id::text, '') ILIKE :transaction_id",
            'account': "COALESCE(pseudo::text, '') ILIKE :account",
            'amount': f'''(
                (debit ~ {AMOUNT_GUARD} AND debit::numeric = :amount)
                OR (credit ~ {AMOUNT_GUARD} AND credit::numeric = :amount)
            )''',
        },
    },
    'cbs': {
        'table': 'interop_om_cbs',
        'columns': (
            ('apiLogId', 'API Log ID'),
            ('PostingDate', 'Date de comptabilisation'),
            ('AmountCRY', 'Montant'),
            ('trx_id', 'Transaction PAMF'),
        ),
        'select': tuple((f'"{column}"', column) for column, _label in (
            ('apiLogId', 'API Log ID'),
            ('PostingDate', 'Date de comptabilisation'),
            ('AmountCRY', 'Montant'),
            ('trx_id', 'Transaction PAMF'),
        )),
        'filters': {
            'transaction_id': '''(
                COALESCE(trx_id::text, '') ILIKE :transaction_id
                OR COALESCE("apiLogId"::text, '') ILIKE :transaction_id
            )''',
            'account': None,
            'amount': '"AmountCRY" = :amount',
        },
    },
}


def list_reconciliation_processes(*, date_from=None, date_to=None, process_id=None, status=None):
    clauses = []
    params = {}
    if date_from:
        clauses.append('p.transaction_date >= :date_from')
        params['date_from'] = date_from
    if date_to:
        clauses.append('p.transaction_date <= :date_to')
        params['date_to'] = date_to
    if process_id:
        clauses.append('p.id = :process_id')
        params['process_id'] = process_id
    if status:
        clauses.append('''EXISTS (
            SELECT 1
            FROM public.interop_om_reconciliation rf
            WHERE rf.process_id = p.id
              AND rf.reconciliation_status = :reconciliation_status
        )''')
        params['reconciliation_status'] = status

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    query = text(f'''
        SELECT
            p.id,
            p."userID" AS user_id,
            p."Status" AS status,
            p."insertDate" AS inserted_at,
            p.transaction_date,
            COUNT(r.id) AS total_count,
            COUNT(r.id) FILTER (WHERE r.reconciliation_status = 'matched') AS matched_count,
            COUNT(r.id) FILTER (WHERE r.reconciliation_status = 'orphan_om') AS orphan_om_count,
            COUNT(r.id) FILTER (WHERE r.reconciliation_status = 'orphan_pamf') AS orphan_pamf_count
        FROM public.interop_om_process p
        LEFT JOIN public.interop_om_reconciliation r ON r.process_id = p.id
        {where_clause}
        GROUP BY p.id, p."userID", p."Status", p."insertDate", p.transaction_date
        ORDER BY p.transaction_date DESC, p.id DESC
    ''')
    with get_pg_engine().connect() as connection:
        return [dict(row) for row in connection.execute(query, params).mappings()]


def get_reconciliation_process(process_id):
    query = text('''
        SELECT id, "userID" AS user_id, "Status" AS status,
               "insertDate" AS inserted_at, transaction_date
        FROM public.interop_om_process
        WHERE id = :process_id
    ''')
    with get_pg_engine().connect() as connection:
        row = connection.execute(query, {'process_id': process_id}).mappings().first()
    return dict(row) if row else None


def get_process_result_rows(
    *, process_id, source, status=None, transaction_id=None, account=None,
    amount=None, limit=50, offset=0,
):
    """Fetch one page of results, or every matching row when limit is None
    (used for exports, which must cover all pages, not just the current one)."""
    source_config = RESULT_SOURCES.get(source)
    if source_config is None:
        raise ValueError('Invalid result source.')

    table = source_config['table']
    selected_columns = ', '.join(
        f'{expression} AS "{alias}"'
        for expression, alias in source_config['select']
    )
    source_filters = source_config.get('filters', {})
    clauses = ['process_id = :process_id']
    params = {'process_id': process_id}
    if source == 'reconciliation' and status:
        clauses.append('reconciliation_status = :status')
        params['status'] = status
    if transaction_id and source_filters.get('transaction_id'):
        clauses.append(source_filters['transaction_id'])
        params['transaction_id'] = f'%{transaction_id.strip()}%'
    if account and source_filters.get('account'):
        clauses.append(source_filters['account'])
        params['account'] = f'%{account.strip()}%'
    if amount is not None and source_filters.get('amount'):
        clauses.append(source_filters['amount'])
        params['amount'] = amount
    where_clause = ' AND '.join(clauses)

    count_query = text(f'SELECT COUNT(*) FROM public.{table} WHERE {where_clause}')
    limit_clause = ''
    if limit is not None:
        params['limit'] = limit
        params['offset'] = offset
        limit_clause = 'LIMIT :limit OFFSET :offset'
    rows_query = text(f'''
        SELECT {selected_columns}
        FROM public.{table}
        WHERE {where_clause}
        ORDER BY id
        {limit_clause}
    ''')
    with get_pg_engine().connect() as connection:
        count_params = {key: value for key, value in params.items() if key not in {'limit', 'offset'}}
        total = connection.execute(count_query, count_params).scalar_one()
        rows = connection.execute(rows_query, params).mappings().all()

    return {
        'columns': source_config['columns'],
        'rows': [dict(row) for row in rows],
        'total': total,
    }
