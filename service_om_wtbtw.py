import pandas as pd
import pyodbc
from pathlib import Path
import os
import platform
from datetime import datetime,date

BASE_DIR = Path(__file__).resolve().parent

input_om = Path(os.getenv('OM_INPUT_DIR', BASE_DIR / 'input' / 'btwtb_om')).resolve()


def get_connection(source):
    if source == 'cbs':
        server = "172.20.24.37"
        database = "CBS"
        username = "Minonja"
        password = "Minonja"
    elif source == 'mapping':
        server = "192.168.123.16"
        database = "BANK_TO_WALLET_OM"
        username = "Minonja"
        password = "Minonja"
    else:
        raise ValueError("Either 'cbs' or 'mapping' must be True.")

    if platform.system() == "Linux":
        driver = "ODBC Driver 17 for SQL Server"
    else:
        driver = "SQL Server"
    print(f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password}")
    return pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password}"
    )
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

def get_om_data_cbs(str_date):
    sql='''
    
    DECLARE @PostingDate DATE = '{d}';

        SELECT
            rc.code,
            al.RequestId,
            act.PostingDate,raut.InsertTime as PostingTime,
            act.DebitCredit * act.AmountCRY as AmountCRY
        FROM CBS.dbo.mcTransaction mc
		JOIN [bagsPAMF_CBS_MC].dbo.apiLog al on al.apiLogId = mc.RequestID 
        JOIN CBS.dbo.accAccountTransaction act ON act.rAutoTransactionID = mc.rAutoTransactionID
        JOIN CBS.dbo.accAccount acc on acc.accAccountID = act.accAccountID
        JOIN CBS.dbo.rContact rc on  acc.rContactID=rc.rContactID
		join cbs.dbo.rAutoTransaction raut on raut.rAutoTransactionID = act.rAutoTransactionID
        WHERE mc.rMerchantID = 30
          AND mc.PostingDate = @PostingDate
		  order by al.apiLogId  asc

    '''.format(d=str_date)
    print(sql)
    connection = get_connection('cbs')

    return pd.read_sql(sql, connection, )
dic_om=read_om_transaction('Daily-ChannelUserTransactionReport-0323600012-20261012.xls')
date_report= dic_om['date']
df_om=dic_om['df']
df_cbs =get_om_data_cbs(date_report)
