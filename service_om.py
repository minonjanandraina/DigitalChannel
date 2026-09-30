
from pathlib import Path
import platform
import os
import pandas as pd
import pyodbc
from datetime import datetime,date
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from functools import lru_cache
from dataclasses import dataclass

BASE_DIR = Path(__file__).resolve().parent

input_om = Path(os.getenv('OM_INPUT_DIR', BASE_DIR / 'input' / 'om')).resolve()



@dataclass
class OmProcess:
    userID: int
    Status: str
    transaction_date: date
    id: int | None = None
    insertDate: datetime | None = None

def create_process(conn, process: OmProcess) -> OmProcess:
    """Insert a row into interop_om_process and fill in id + insertDate."""
    row = conn.execute(
        text('''
            INSERT INTO interop_om_process ("userID", "Status", "transaction_date")
            VALUES (:userID, :Status, :transaction_date)
            RETURNING id, "insertDate"
        '''),
        {
            "userID": process.userID,
            "Status": process.Status,
            "transaction_date": process.transaction_date,
        },
    ).one()

    process.id = row.id
    process.insertDate = row.insertDate
    return process

@lru_cache(maxsize=1)
def get_pg_engine():
    database_url = "postgresql://admin_digital_chanel:Raz12Min%40%40@192.168.123.97:5432/digital_chanel_db"
    if not database_url:
        raise RuntimeError('DATABASE_URL must be configured to run Orange Money reconciliation.')
    url = make_url(database_url)
    if url.get_backend_name() != 'postgresql':
        raise RuntimeError('DATABASE_URL must point to PostgreSQL for Orange Money reconciliation.')
    return create_engine(url.set(drivername='postgresql+psycopg2'))

def read_om_transaction(filename):
    

    #columns om
    cols_om=['No','date','hour','trx_id','service','transaction_type','status','mode','technical_account','wallet_technical_account','pseudo','client_account','wallet_type','debit','credit','commission_amount','sous_reseau']
    # test readin the excel file
    df_om = pd.read_excel(input_om / filename,names=cols_om)
    df_om_success= df_om.copy()
    df_om_success=df_om_success[df_om_success['status']=='Succès']
    
    # get date
    date_string=filename[46:][:8]
    formatted_date = datetime.strptime(date_string, "%Y%m%d").date()#2026-09-07
    
    #print date type
    
    return {'df':df_om_success,'date':formatted_date.isoformat()}

def get_connection():
    server = "172.20.24.37"
    database = "CBS"
    username = "Minonja"
    password = "Minonja"
    if platform.system() == "Linux":
        driver = "ODBC Driver 17 for SQL Server"
    else:
        driver = "SQL Server"
    return pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password}"
    )

def get_om_data_cbs(str_date):
    sql='''
    SET NOCOUNT ON;
    DECLARE @PostingDate DATE = '{d}';

    WITH transaction_table AS (
        -- Loan repayment
        SELECT
            mc.RequestID AS apiLogId,
            'loan_repayment' AS trx_type,
            lc.PostingDate,
            lc.AmountCRY
        FROM CBS.dbo.mcTransaction mc
        JOIN CBS.dbo.loLoanCredit lc
            ON lc.rAutoTransactionID = mc.rAutoTransactionID
        WHERE mc.rMerchantID = 9
          AND mc.PostingDate = @PostingDate

        UNION ALL

        -- Account deposit
        SELECT
            mc.RequestID AS apiLogId,
            'account_deposit'  AS trx_type,
            act.PostingDate,
            act.AmountCRY
        FROM CBS.dbo.mcTransaction mc
        JOIN CBS.dbo.accAccountTransaction act
            ON act.rAutoTransactionID = mc.rAutoTransactionID
        WHERE mc.rMerchantID = 9
          AND mc.PostingDate = @PostingDate
    )
    SELECT apiLogId,PostingDate,sum(AmountCRY) AS AmountCRY 
    FROM transaction_table
    group by apiLogId,PostingDate;
    '''.format(d=str_date)
    connection = get_connection()

    return pd.read_sql(sql, connection, )

def get_trx_id_cbs(apiLogid):
    sql = '''
        SELECT TOP 1
            al.apiLogId AS RequestID,
            al.RequestId AS trx_id
        FROM [bagsPAMF_CBS_MC].dbo.apiLog al
        WHERE  apiLogId = ?
    '''

    connection = get_connection()
    try:
        df = pd.read_sql(sql, connection, params=[apiLogid])
    finally:
        connection.close()

    if df.empty:
        return None

    return df.iloc[0]['trx_id']

def delete_existing_process_data(date_str):
    """Delete existing data for a given transaction_date from the four tables."""

    engine = create_engine("postgresql://admin_digital_chanel:Raz12Min%40%40@192.168.123.97:5432/digital_chanel_db")

    with engine.begin() as conn:
        conn.execute(text("""
                    
                    DELETE FROM public.om_orphanomprocessinghistory
                    WHERE "InteropOmReconciliation_id" IN (
                        SELECT r.id FROM 
                        public.interop_om_process p
                        join public.interop_om_reconciliation r on r.process_id  =p.id
                        WHERE p.transaction_date =  '{dt}'
                    )
        
                    
                """.format(dt=date_str)))
        
        conn.execute(text("""
            DELETE FROM public.interop_om_reconciliation
            WHERE process_id IN (
                SELECT id FROM public.interop_om_process
                WHERE transaction_date = '{dt}'
            )
        """.format(dt=date_str)))

        conn.execute(text("""
            DELETE FROM public.interop_om_cbs
            WHERE process_id IN (
                SELECT id FROM public.interop_om_process
                WHERE transaction_date = '{dt}'
            )
        """.format(dt=date_str)))

        conn.execute(text("""
            DELETE FROM public.interop_om_om
            WHERE process_id IN (
                SELECT id FROM public.interop_om_process
                WHERE transaction_date = '{dt}'
            )
        """.format(dt=date_str)))

        conn.execute(text("""
            DELETE FROM public.interop_om_process
            WHERE transaction_date = '{dt}'
        """.format(dt=date_str)))

    return {
        "status": "success",
        "message": f"Existing data for transaction_date {date_str} deleted successfully."
    } 

def reconciliation(filename,user_id):
#    filename='Daily-ChannelUserTransactionReport-0324660679-20260912.xls'

    try:
        om_dic=read_om_transaction(filename)
        df_om_om= om_dic['df']
        transaction_date = om_dic['date']
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error reading OM transaction file: {str(e)}"
        }
    try:
    #delete existing process
        delete_existing_process_data(transaction_date)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error deleting existing process data: {str(e)}"
        }
        
    try:
        df_om_cbs =  get_om_data_cbs(transaction_date)
        df_om_cbs_trx_id=df_om_cbs.copy()
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error fetching OM data from CBS: {str(e)}"
        }
        
    try:
        df_om_cbs_trx_id['trx_id'] = df_om_cbs_trx_id['apiLogId'].apply(
            lambda x: get_trx_id_cbs(x)
        )# key to join with df_mvola: trx_id
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error fetching requestId from CBS: {str(e)}"
        }
    
    try:
        df_om_merged = pd.merge(
            df_om_om, df_om_cbs_trx_id,
            how='outer', left_on='trx_id', right_on='trx_id',
            indicator=True
        )
        df_om_merged['reconciliation_status'] = df_om_merged['_merge'].map({
            'left_only':  'orphan_om',
            'right_only': 'orphan_pamf',
            'both':       'matched',
        }).astype(str)
        
        df_om_merged = df_om_merged.drop(columns='_merge')
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error merging OM and CBS data: {str(e)}"
        }
    
    try:
        with get_pg_engine().begin() as conn:
            process = create_process(conn, OmProcess(
                userID=user_id, Status="completed", transaction_date=transaction_date,
            ))
            for df, table in [
                        (df_om_om,            'interop_om_om'),
                        (df_om_cbs_trx_id, 'interop_om_cbs'),
                        (df_om_merged,     'interop_om_reconciliation'),
                    ]:
                df.assign(process_id=process.id).to_sql(table, conn, if_exists='append', index=False)
        return {
            "status":"success",
            "message":f"Data inserted successfully for transaction_date {transaction_date}.",
            "process_id": process.id,
        }
    except Exception as e:
        return {
            "status":"error",
            "message":f"Error inserting data into PostgreSQL: {str(e)}"
        }


if __name__ == "__main__":
    output=reconciliation('Daily-ChannelUserTransactionReport-0324660679-20260907.xls',1)#default 1