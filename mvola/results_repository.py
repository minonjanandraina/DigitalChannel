from sqlalchemy import text

from service_mvola import get_pg_engine


RESULT_SOURCES = {
    'reconciliation': {
        'table': 'interop_mvola_reconciliation',
        'columns': (
            ('transaction_ids', 'Transaction MVOLA / PAMF'),
            ('reconciliation_status', 'Statut'),
            ('DATE_TRANS', 'Date MVOLA'),
            ('PostingDate', 'Date PAMF'),
            ('MSISDN', 'Téléphone'),
            ('amounts', 'Montant MVOLA / PAMF'),
        ),
        'select': (
            ('id', 'id'),
            ('''concat_ws(' / ', NULLIF("TRANSID_MVOLA"::text, ''), NULLIF(trx_id::text, ''))''', 'transaction_ids'),
            ('"reconciliation_status"', 'reconciliation_status'),
            ('"DATE_TRANS"', 'DATE_TRANS'),
            ('"PostingDate"', 'PostingDate'),
            ('"MSISDN"', 'MSISDN'),
            ('''concat_ws(' / ', NULLIF("AMOUNT"::text, ''), NULLIF("AmountCRY"::text, ''))''', 'amounts'),
        ),
        'filters': {
            'transaction_id': '''(
                COALESCE("TRANSID_MVOLA"::text, '') ILIKE :transaction_id
                OR COALESCE(trx_id::text, '') ILIKE :transaction_id
            )''',
            'phone': 'COALESCE("MSISDN"::text, \'\') ILIKE :phone',
            'amount': '''(
                CAST("AMOUNT" AS numeric) = :amount
                OR "AmountCRY" = :amount
            )''',
        },
    },
    'mvola': {
        'table': 'interop_mvola_mvola',
        'columns': (
            ('TRANSID_MVOLA', 'Transaction MVOLA'),
            ('DATE_TRANS', 'Date'),
            ('STATE', 'État MVOLA'),
            ('MSISDN', 'Téléphone'),
            ('NOM', 'Nom'),
            ('AMOUNT', 'Montant'),
            ('TYPE_OPERATION', 'Opération'),
        ),
        'select': tuple((f'"{column}"', column) for column, _label in (
            ('TRANSID_MVOLA', 'Transaction MVOLA'),
            ('DATE_TRANS', 'Date'),
            ('STATE', 'État MVOLA'),
            ('MSISDN', 'Téléphone'),
            ('NOM', 'Nom'),
            ('AMOUNT', 'Montant'),
            ('TYPE_OPERATION', 'Opération'),
        )),
        'filters': {
            'transaction_id': 'COALESCE("TRANSID_MVOLA"::text, \'\') ILIKE :transaction_id',
            'phone': 'COALESCE("MSISDN"::text, \'\') ILIKE :phone',
            'amount': 'CAST("AMOUNT" AS numeric) = :amount',
        },
    },
    'cbs': {
        'table': 'interop_mvola_cbs',
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
            'phone': None,
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
            FROM public.interop_mvola_reconciliation rf
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
            COUNT(r.id) FILTER (
                WHERE r.reconciliation_status = 'orphan_mvola' AND COALESCE(h.status, '') <> 'done'
            ) AS orphan_mvola_count,
            COUNT(r.id) FILTER (
                WHERE r.reconciliation_status = 'orphan_pamf' AND COALESCE(h.status, '') <> 'done'
            ) AS orphan_pamf_count
        FROM public.interop_mvola_process p
        LEFT JOIN public.interop_mvola_reconciliation r ON r.process_id = p.id
        LEFT JOIN LATERAL (
            SELECT oh.status
            FROM public.mvola_orphanmvolaprocessinghistory oh
            WHERE oh."InteropMvolaReconciliation_id" = r.id
            ORDER BY oh.processed_at DESC
            LIMIT 1
        ) h ON true
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
        FROM public.interop_mvola_process
        WHERE id = :process_id
    ''')
    with get_pg_engine().connect() as connection:
        row = connection.execute(query, {'process_id': process_id}).mappings().first()
    return dict(row) if row else None


def get_process_result_rows(
    *, process_id, source, status=None, transaction_id=None, phone=None,
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
    if phone and source_filters.get('phone'):
        clauses.append(source_filters['phone'])
        params['phone'] = f'%{phone.strip()}%'
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