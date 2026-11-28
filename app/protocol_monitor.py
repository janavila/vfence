"""Local read-only monitor. No operational DB, serial or FPGA imports."""
from __future__ import annotations
import csv, io, json, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from app.packet_journal import DB_PATH
from app.packet_decoder import inspect_packet


def local_only(request: Request):
    if request.client is None or request.client.host not in ('127.0.0.1','::1','::ffff:127.0.0.1'):
        raise HTTPException(403,'Monitor disponível somente no computador da Base')

router=APIRouter(dependencies=[Depends(local_only)])
PAGE=Path(__file__).resolve().parent/'templates'/'protocol_monitor.html'
COLS='id,session,at,kind,direction,collar,type,subtype,profile,sequence,retry,valid,rssi,snr,satellites,hdop,pdr,battery,distance,ordinal,operation,phase,gateway_session,source_profile,target_profile,wire_type,wire_subtype'


def read_db():
    if not DB_PATH.is_file():return None
    con=sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    con.row_factory=sqlite3.Row;con.execute('PRAGMA query_only=ON');return con


def query(sql,params=()):
    con=read_db()
    if con is None:return []
    try:
        with closing(con):return [dict(r) for r in con.execute(sql,params)]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return [] # during first creation
        raise HTTPException(503,'Banco de captura temporariamente indisponível') from exc


def iso(s):
    if not s:return None
    try:
        d=datetime.fromisoformat(s.replace('Z','+00:00'))
        if d.tzinfo is None:raise ValueError()
        return d.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    except ValueError as exc:raise HTTPException(422,'Data inválida; informe fuso horário') from exc


def conditions(session:str='',collar:int|None=Query(None,ge=1,le=254),start:str='',end:str='',profile:int|None=Query(None,ge=0,le=3)):
    clauses=[];args=[]
    for field,val,op in [('session',session or None,'='),('collar',collar,'='),('at',iso(start),'>='),('at',iso(end),'<='),('profile',profile,'=')]:
        if val is not None:clauses.append(f'{field}{op}?');args.append(val)
    return clauses,args


def packet_conditions(base=Depends(conditions),category:str='all',subtype:str='',direction:str='',retry:bool=False,invalid:bool=False,sequence:int|None=Query(None,ge=0,le=65535)):
    clauses,args=base;clauses=list(clauses);args=list(args);clauses.append("kind='packet_log'")
    if category=='confirm':clauses.append("subtype IN ('ACK','NACK')")
    elif category=='profile':clauses.append("subtype='PROFILE_CONTROL'")
    elif category in ('CONFIG','EVENT','TELEMETRY','CONTROL'):clauses.append('type=?');args.append(category)
    elif category!='all':raise HTTPException(422,'Categoria inválida')
    if direction:
        if direction not in ('TX','RX'):raise HTTPException(422,'Direção inválida')
        clauses.append('direction=?');args.append(direction)
    if subtype=='METRIC_UPLINK':clauses.append("(wire_subtype='METRIC_UPLINK' OR subtype='METRIC_UPLINK')")
    elif subtype:clauses.append('subtype=?');args.append(subtype)
    if retry:clauses.append('retry=1')
    if invalid:clauses.append('valid=0')
    if sequence is not None:clauses.append('sequence=?');args.append(sequence)
    return ' WHERE '+' AND '.join(clauses),args


def where(base,extra):
    return ' WHERE '+' AND '.join([*base[0],extra]),list(base[1])

@router.get('/monitor',include_in_schema=False)
def page():return FileResponse(PAGE,headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"})

@router.get('/api/protocol-monitor/options')
def options():
    return {'sessions':query('SELECT * FROM capture_sessions ORDER BY started DESC LIMIT 2000'), 'collars':[r['collar'] for r in query('SELECT DISTINCT collar FROM packets WHERE collar BETWEEN 1 AND 254 ORDER BY collar')], 'subtypes':[r['subtype'] for r in query("SELECT subtype FROM packets WHERE kind='packet_log' AND subtype IS NOT NULL UNION SELECT wire_subtype AS subtype FROM packets WHERE kind='packet_log' AND wire_subtype='METRIC_UPLINK' ORDER BY subtype")], 'imports':query('SELECT * FROM imported_files ORDER BY name')}

@router.get('/api/protocol-monitor/packets')
def packets(filters=Depends(packet_conditions),limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0,le=1000000)):
    w,args=filters
    rows=query(f'SELECT {COLS} FROM packets{w} ORDER BY at DESC,id DESC LIMIT ? OFFSET ?',(*args,limit+1,offset))
    return {'rows':rows[:limit],'more':len(rows)>limit}

@router.get('/api/protocol-monitor/packets/{packet_id}')
def packet(packet_id:int):
    rows=query('SELECT * FROM packets WHERE id=? AND kind=?',(packet_id,'packet_log'))
    if not rows:raise HTTPException(404,'Pacote não encontrado')
    row=rows[0];row['record']=json.loads(row['record']);row['inspection']=inspect_packet(row['record'].get('rawHex'));return row

@router.get('/api/protocol-monitor/metrics')
def metrics(base=Depends(conditions),limit:int=Query(1500,ge=10,le=10000)):
    # Separate series prevent summaries being mixed with individual packet measurements.
    w,args=where(base,"kind='packet_log' AND direction='RX' AND (rssi IS NOT NULL OR snr IS NOT NULL OR satellites IS NOT NULL OR hdop IS NOT NULL OR battery IS NOT NULL OR distance IS NOT NULL)")
    fields='id,at,collar,profile,rssi,snr,satellites,hdop,battery,distance,pdr,subtype,session'
    raw=query(f'SELECT {fields} FROM packets{w} ORDER BY at DESC,id DESC LIMIT ?',(*args,limit+1))
    w,args=where(base,"kind IN ('radio_window_summary','hdop_window_summary','pdr_window_summary')")
    summaries=query(f'SELECT {fields} FROM packets{w} ORDER BY at DESC,id DESC LIMIT ?',(*args,limit+1))
    w,args=where(base,"kind='packet_log' AND subtype='PROFILE_CONTROL' AND phase IN ('VERIFIED','RESTORED') AND valid=1 AND direction='RX'")
    marks=query(f'SELECT id,at,collar,profile,phase,operation,session FROM packets{w} ORDER BY at DESC,id DESC LIMIT 500',args)
    return {'samples':list(reversed(raw[:limit])),'summaries':list(reversed(summaries[:limit])),'marks':marks,'truncated':len(raw)>limit or len(summaries)>limit}

@router.get('/api/protocol-monitor/transitions')
def transitions(base=Depends(conditions)):
    # The radio profile filter is deliberately ignored for full operation context.
    clauses,args=base
    w=' WHERE '+' AND '.join([*clauses,"kind='packet_log'","subtype='PROFILE_CONTROL'","operation IS NOT NULL","valid=1"])
    rows=query(f'SELECT {COLS} FROM packets{w} ORDER BY at DESC,id DESC LIMIT 5001',args)
    groups={}
    for r in reversed(rows[:5000]):
        key=(r['gateway_session'],r['operation'],r['collar']);groups.setdefault(key,[]).append(r)
    return {'groups':list(reversed(list(groups.values()))),'truncated':len(rows)>5000}

@router.get('/api/protocol-monitor/compare')
def compare(base=Depends(conditions)):
    w,args=where(base,"kind='packet_log'")
    rows=query(f'''SELECT session,profile,collar,count(*) AS packets,sum(direction='RX') AS rx,sum(direction='TX') AS tx,sum(coalesce(retry,0)) AS retries,round(avg(rssi),2) AS rssi,round(avg(snr),2) AS snr,round(avg(satellites),2) AS satellites FROM packets{w} GROUP BY session,profile,collar ORDER BY max(at) DESC LIMIT 501''',args)
    total=query(f"SELECT count(*) AS packets,coalesce(sum(direction='RX'),0) AS rx,coalesce(sum(direction='TX'),0) AS tx,coalesce(sum(retry),0) AS retries,round(avg(rssi),2) AS rssi,round(avg(snr),2) AS snr FROM packets{w}",args)
    return {'rows':rows[:500],'truncated':len(rows)>500,'total':total[0] if total else {}}

@router.get('/api/protocol-monitor/export')
def export(filters=Depends(packet_conditions)):
    w,args=filters
    def stream():
        buffer=io.StringIO();writer=csv.writer(buffer);header=COLS.split(',')+['rawHex','record_json'];writer.writerow(header);yield '\ufeff'+buffer.getvalue();buffer.seek(0);buffer.truncate(0)
        con=read_db()
        if con is None:return
        with closing(con):
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packets'").fetchone() is None:return
            cursor=con.execute(f'SELECT {COLS},record FROM packets{w} ORDER BY at,id',args)
            for row in cursor:
                rec=json.loads(row['record']);writer.writerow([row[k] for k in header[:-2]]+[rec.get('rawHex'),row['record']]);yield buffer.getvalue();buffer.seek(0);buffer.truncate(0)
    return StreamingResponse(stream(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="vfence_packets_filtrados.csv"'})
