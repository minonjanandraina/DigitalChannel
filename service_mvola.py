# -*- coding: utf-8 -*-
"""
Created on Mon Sep 28 08:59:59 2026

@author: m.razakasoa
"""
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import os
import pandas as pd
import platform
import pyodbc
from pathlib import Path

import urllib

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env', override=False)
input_mvola = Path(os.getenv('MVOLA_INPUT_DIR', BASE_DIR / 'input' / 'mvola')).resolve()
server = "172.20.24.37"
database = "CBS"
username = "Minonja"
password = "Minonja"

if platform.system() == "Linux":
    driver = "ODBC Driver 17 for SQL Server"
else:
    driver = "SQL Server"




DATABASE_URL = "postgresql://admin_digital_chanel:Raz12Min%40%40@192.168.123.97:5432/digital_chanel_db"


@lru_cache(maxsize=1)
def get_pg_engine():
    database_url = os.getenv('DATABASE_URL', '').strip()
    if not database_url:
        raise RuntimeError('DATABASE_URL must be configured to run MVOLA reconciliation.')
    url = make_url(database_url)
    if url.get_backend_name() != 'postgresql':
        raise RuntimeError('DATABASE_URL must point to PostgreSQL for MVOLA reconciliation.')
    return create_engine(url.set(drivername='postgresql+psycopg2'))
@dataclass
class MvolaProcess:
    userID: int
    Status: str
    transaction_date: date
    id: int | None = None
    insertDate: datetime | None = None

def create_process(conn, process: MvolaProcess) -> MvolaProcess:
    """Insert a row into interop_mvola_process and fill in id + insertDate."""
    row = conn.execute(
        text('''
            INSERT INTO interop_mvola_process ("userID", "Status", "transaction_date")
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

def check_filename(filename):
    # Expected format: YYYY-MM-DD_reporting_PAMF.csv
    if len(filename) != 29:
        return False

    if filename[10:] != "_reporting_PAMF.csv":
        return False

    try:
        pd.to_datetime(filename[:10], format="%Y-%m-%d")
        return True
    except ValueError:
        return False

def read_mvola_file(filename):
    if check_filename(filename):
        print(filename)
        df_mvola = pd.read_csv(input_mvola / filename, sep=';')
        #filter Completed
        df_mvola = df_mvola[df_mvola['STATE'] == 'Completed']
        return df_mvola
    else:
        print(f"INVALID : {filename}")
        return None

def get_trx_id_cbs(apiLogid):
    sql = '''
        SELECT TOP 1
            al.apiLogId AS RequestID,
            al.RequestId AS trx_id
        FROM [bagsPAMF_CBS_MC].dbo.apiLog al
        WHERE al.rMerchantId = 13
          AND apiServiceId IN (302, 700)
          AND apiLogId = ?
    '''

    connection = get_connection()
    try:
        df = pd.read_sql(sql, connection, params=[apiLogid])
    finally:
        connection.close()

    if df.empty:
        return None

    return df.iloc[0]['trx_id']

def get_mvola_data_cbs(str_date):
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
        WHERE mc.rMerchantID = 13
          AND mc.PostingDate = @PostingDate

        UNION ALL

        -- Account deposit
        SELECT
            mc.RequestID,
            'account_deposit',
            act.PostingDate,
            act.AmountCRY
        FROM CBS.dbo.mcTransaction mc
        JOIN CBS.dbo.accAccountTransaction act
            ON act.rAutoTransactionID = mc.rAutoTransactionID
        WHERE mc.rMerchantID = 13
          AND mc.PostingDate = @PostingDate
    )
    SELECT apiLogId,PostingDate,sum(AmountCRY) AS AmountCRY 
    FROM transaction_table
    group by apiLogId,PostingDate;
    '''.format(d=str_date)
    connection = get_connection()

    return pd.read_sql(sql, connection, )

def delete_existing_process_data(date_str):
    """Delete existing data for a given transaction_date from the four tables."""

    engine = create_engine(DATABASE_URL)

    with engine.begin() as conn:
        conn.execute(text("""
            
            DELETE FROM public.mvola_orphanmvolaprocessinghistory
            WHERE "InteropMvolaReconciliation_id" IN (
                SELECT r.id FROM 
                public.interop_mvola_process p
                join public.interop_mvola_reconciliation r on r.process_id  =p.id
                WHERE p.transaction_date =  '{dt}'

            )
        """.format(dt=date_str)))
        
        conn.execute(text("""
            DELETE FROM public.interop_mvola_reconciliation
            WHERE process_id IN (
                SELECT id FROM public.interop_mvola_process
                WHERE transaction_date = '{dt}'
            )
        """.format(dt=date_str)))

        conn.execute(text("""
            DELETE FROM public.interop_mvola_cbs
            WHERE process_id IN (
                SELECT id FROM public.interop_mvola_process
                WHERE transaction_date = '{dt}'
            )
        """.format(dt=date_str)))

        conn.execute(text("""
            DELETE FROM public.interop_mvola_mvola
            WHERE process_id IN (
                SELECT id FROM public.interop_mvola_process
                WHERE transaction_date = '{dt}'
            )
        """.format(dt=date_str)))

        conn.execute(text("""
            DELETE FROM public.interop_mvola_process
            WHERE transaction_date = '{dt}'
        """.format(dt=date_str)))

    return {
        "status": "success",
        "message": f"Existing data for transaction_date {date_str} deleted successfully."
    } 

def reconciliation(file,userid):
    print('*'*60)
    print(f"Starting reconciliation for file: {file} by user: {userid}")
    print('*'*60)
    str_date=file[:10]

    delete_existing_process_data( str_date)
    df_mvola=read_mvola_file(file)# key to join with df_cbs_mvola : TRANSID_MVOLA 
    if df_mvola is None:
        ctx = {
            "status": "error",
            "message": f"Invalid filename: {file}. Expected format: YYYY-MM-DD_reporting_PAMF.csv"
        }
        return ctx
    df_mvola_cbs=get_mvola_data_cbs(str_date)
    if df_mvola_cbs.empty:
        ctx = {
            "status": "error",
            "message": f"No data found in CBS for the date: {str_date}"
        }
        return ctx
    
    df_mvola_cbs_trx_id=df_mvola_cbs.copy()
    #application of get_trx_id_cbs using lambda function in df_mvola_cbs
    
    try:    
        df_mvola_cbs_trx_id['trx_id'] = df_mvola_cbs_trx_id['apiLogId'].apply(
            lambda x: get_trx_id_cbs(x)
        )# key to join with df_mvola: trx_id
    except Exception as e:
        ctx = {
            "status": "error",
            "message": f"Error while fetching trx_id from CBS: {str(e)}"
        }
        return ctx

    df_mvola['TRANSID_MVOLA'] = df_mvola['TRANSID_MVOLA'].astype(str)
    df_mvola_cbs_trx_id['trx_id'] = df_mvola_cbs_trx_id['trx_id'].astype(str)

    try:
        df_mvola_merged = pd.merge(
            df_mvola, df_mvola_cbs_trx_id,
            how='outer', left_on='TRANSID_MVOLA', right_on='trx_id',
            indicator=True
        )
    except Exception as e:
        ctx = {
            "status": "error",
            "message": f"Error while merging data: {str(e)}"
        }
        return ctx

    try:
        df_mvola_merged['reconciliation_status'] = df_mvola_merged['_merge'].map({
            'left_only':  'orphan_mvola',
            'right_only': 'orphan_pamf',
            'both':       'matched',
        }).astype(str)
        df_mvola_merged = df_mvola_merged.drop(columns='_merge')
    except Exception as e:
        ctx = {
            "status": "error",
            "message": f"Error while mapping reconciliation status: {str(e)}"
        }
        return ctx

    try:
    # send data to the db
        reco_date = date.fromisoformat(str_date)
        with get_pg_engine().begin() as conn:
            process = create_process(conn, MvolaProcess(
                userID=userid, Status="completed", transaction_date=reco_date,
            ))
            print(process.id, process.insertDate)

            for df, table in [
                (df_mvola,            'interop_mvola_mvola'),
                (df_mvola_cbs_trx_id, 'interop_mvola_cbs'),
                (df_mvola_merged,     'interop_mvola_reconciliation'),
            ]:
                df.assign(process_id=process.id).to_sql(
                    table, conn, if_exists='append', index=False)
        ctx = {
            "status": "success",
            "message": f"Data successfully inserted into the database for the date: {str_date}",
            "process_id": process.id,
            "df":[df_mvola,df_mvola_cbs_trx_id,df_mvola_merged]
        }
        return ctx
    except Exception as e:
        ctx = {
            "status": "error",
            "message": f"Error while inserting data into the database: {str(e)}"
        }
        return ctx

def get_transaction_by_requestID(requestID):
    sql='''
    select top 1 requestID, RequestBody, RequestURL, ResponseBody from [bagsPAMF_CBS_MC].dbo.apiLog where requestID='{r}' 
    order by apiLogId desc
    '''.format(r=requestID)
    df = pd.read_sql(sql, get_connection())
    
    if len(df) == 0:
        return None
    else:
        #to dictionary record
        return df.to_dict(orient='records')[0]
    
if __name__ == "__main__":
    output=reconciliation('2026-09-19_reporting_PAMF.csv',1)#default 1