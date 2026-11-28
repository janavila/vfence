"""Persistent, independent SQLite capture. Reader never waits for disk."""
from __future__ import annotations
import csv, json, logging, queue, sqlite3, threading, hashlib, math
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from contextlib import closing
from app.packet_decoder import inspect_packet
from app.pdr_protocol import parse_pdr_summary
from app.metric_protocol import parse_metric_summary

logger=logging.getLogger(__name__)
DB_PATH=Path(__file__).resolve().parents[1]/'logs'/'vfence_packets.db'
_PACKET_CSV_FIELDS = (
    'hostTimeUtc','gatewaySessionId','gatewayMs','direction','profileId','length','valid',
    'version','type','typeId','subtype','subtypeId','collarId','sequence','ackReq','retry','urgent',
    'rssiDbm','snrDb','txOk','txBytesWritten','collarSessionId','positionOrdinal','packetProfileId',
    'profileStep','profileSource','profileTarget','profileOperationId','profileWaitMs','rawHex',
)

def utc(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def connect(path):
    con=sqlite3.connect(path,timeout=5); con.row_factory=sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000'); return con

def initialize(path):
    with closing(connect(path)) as con, con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''CREATE TABLE IF NOT EXISTS capture_sessions(id TEXT PRIMARY KEY,started TEXT NOT NULL,ended TEXT,source TEXT NOT NULL,dropped INTEGER DEFAULT 0,failed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS packets(id INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT NOT NULL,at TEXT NOT NULL,kind TEXT NOT NULL,direction TEXT,collar INTEGER,type TEXT,subtype TEXT,profile INTEGER,sequence INTEGER,retry INTEGER,valid INTEGER,rssi REAL,snr REAL,satellites INTEGER,hdop REAL,pdr REAL,battery REAL,distance REAL,ordinal INTEGER,operation INTEGER,phase TEXT,gateway_session TEXT,source_profile INTEGER,target_profile INTEGER,record TEXT NOT NULL,import_key TEXT UNIQUE);
        CREATE INDEX IF NOT EXISTS packets_time ON packets(at,id);
        CREATE INDEX IF NOT EXISTS packets_session ON packets(session,id);
        CREATE INDEX IF NOT EXISTS packets_collar ON packets(collar,at);
        CREATE INDEX IF NOT EXISTS packets_kind ON packets(kind,type,subtype,id);
        CREATE INDEX IF NOT EXISTS packets_operation ON packets(gateway_session,operation,collar,id);
        CREATE TABLE IF NOT EXISTS imported_files(name TEXT PRIMARY KEY,digest TEXT,rows INTEGER,errors INTEGER,imported_at TEXT);''')
        columns={r['name'] for r in con.execute('PRAGMA table_info(packets)')}
        for name in ('wire_type','wire_subtype'):
            if name not in columns:con.execute(f'ALTER TABLE packets ADD COLUMN {name} TEXT')
        con.execute('CREATE INDEX IF NOT EXISTS packets_wire ON packets(wire_subtype,id)')

_COLUMNS='session at kind direction collar type subtype profile sequence retry valid rssi snr satellites hdop pdr battery distance ordinal operation phase gateway_session source_profile target_profile record import_key wire_type wire_subtype'.split()
_SQL='INSERT OR IGNORE INTO packets('+','.join(_COLUMNS)+') VALUES ('+','.join('?' for _ in _COLUMNS)+')'

def number(v):
    return v if type(v) in (int,float) and math.isfinite(v) and abs(v)<2**63 else None

def string(v): return v if isinstance(v,str) else None

def row_for(record,session,key=None):
    kind=record.get('kind','packet_log'); ts=record.get('hostTimeUtc')
    if not isinstance(ts,str): raise ValueError('timestamp ausente')
    dt=datetime.fromisoformat(ts.replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('timestamp sem fuso')
    ts=dt.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    r=dict(record,hostTimeUtc=ts); d={};wire_type=wire_subtype=None;retry=r.get('retry')
    if kind=='packet_log':
        inspected=inspect_packet(r.get('rawHex')); d=inspected['decoded'] if inspected['status']=='decoded' and r.get('valid') is not False else {}
        wire_type=r.get('type') or d.get('wireType') or d.get('type'); wire_subtype=r.get('subtype') or d.get('wireSubtype') or d.get('subtype')
        typ,sub=wire_type,wire_subtype
        if d.get('wireSubtype')=='METRIC_UPLINK':
            typ,sub=d['type'],d['subtype'];retry=d['controle']['retry']
        position=d.get('subtype') in ('POSITION_STATUS','POSITION_STATUS_COUNTED')
        # Do not fabricate GNSS values from a packet with invalid GPS.
        gps=position and d.get('stateFlags',{}).get('gpsValid') is True
        rx=r.get('direction')=='RX'; rssi=number(r.get('rssiDbm')) if rx else None; snr=number(r.get('snrDb')) if rx else None
        sats=number(d.get('satellites')) if gps and rx else None; hdop=number(d.get('hdop')) if gps and rx else None
        battery=number(d.get('batteryPct')) if position and rx else None; distance=number(d.get('distanceBorderM')) if gps and rx else None
        pdr=None; phase=d.get('profileStep'); op=number(d.get('profileOperationId')); source=number(d.get('profileSource')); target=number(d.get('profileTarget'))
    elif kind=='pdr_window_summary':
        p=parse_pdr_summary({k:v for k,v in r.items() if k!='hostTimeUtc'}); typ='SUMMARY';sub='PDR';pdr=p.pdr_x100/100 if p.valid else None
        rssi=snr=sats=hdop=battery=distance=phase=op=source=target=None
    elif kind in ('radio_window_summary','hdop_window_summary'):
        m=parse_metric_summary({k:v for k,v in r.items() if k!='hostTimeUtc'},'');typ='SUMMARY';sub=m.kind
        rssi=m.rssi_mean_x100/100 if m.rssi_mean_valid else None;snr=m.snr_mean_x100/100 if m.snr_mean_valid else None;hdop=m.hdop_mean_x100/100 if m.hdop_mean_valid else None
        pdr=sats=battery=distance=phase=op=source=target=None
    else: raise ValueError('kind não suportado')
    return (session,ts,kind,string(r.get('direction')),number(r.get('collarId',d.get('collarId'))),string(typ),string(sub),number(r.get('profileId')),number(r.get('sequence',d.get('sequence'))),int(retry) if type(retry) is bool else None,int(r['valid']) if type(r.get('valid')) is bool else None,rssi,snr,sats,hdop,pdr,battery,distance,number(d.get('positionOrdinal')),op,string(phase),string(r.get('gatewaySessionId')),source,target,json.dumps(r,ensure_ascii=False,separators=(',',':'),allow_nan=False),key,string(wire_type),string(wire_subtype))

# CSVs produced by the earlier journal use Python bool spellings; JSONL is preferred.
def csv_record(row):
    strings={'hostTimeUtc','gatewaySessionId','direction','type','subtype','collarSessionId','profileStep','rawHex'}
    bools={'valid','ackReq','retry','urgent','txOk'}
    out={'kind':'packet_log','schema':'vfence.gateway.serial.v1'}
    for k,v in row.items():
        if not v: continue
        if k in strings:out[k]=v
        elif k in bools:
            if v.lower() not in ('true','false','1','0'):raise ValueError('booleano CSV inválido')
            out[k]=v.lower() in ('true','1')
        elif k in ('rssiDbm','snrDb'):out[k]=float(v)
        elif k in _PACKET_CSV_FIELDS:out[k]=int(v)
    return out

def import_history(path,stop=None):
    """Import old files, once per file content + row; preserve identical live attempts.
    CSV companion skipped when its matching JSONL exists; source files never changed.
    """
    files=sorted(path.parent.glob('vfence_packets_*.jsonl'))+sorted(p for p in path.parent.glob('vfence_packets_*.csv') if not p.with_suffix('.jsonl').exists())
    with closing(connect(path)) as con:
        for file in files:
            if stop and stop.is_set():return
            digest=hashlib.sha256()
            with file.open('rb') as stream:
                for chunk in iter(lambda:stream.read(65536),b''):digest.update(chunk)
            signature=digest.hexdigest(); prev=con.execute('SELECT digest FROM imported_files WHERE name=?',(file.name,)).fetchone()
            if prev and prev[0]==signature:continue
            # Ignore a renamed, byte-identical copy of an already imported source.
            duplicate=con.execute('SELECT 1 FROM imported_files WHERE digest=? AND name<>?',(signature,file.name)).fetchone()
            if duplicate:
                with con:con.execute('INSERT OR REPLACE INTO imported_files VALUES(?,?,?,?,?)',(file.name,signature,0,0,utc()))
                continue
            # Stable by file name/line, so appending an old file doesn't reimport its prefix.
            session='import-'+hashlib.sha256(file.stem.encode()).hexdigest()[:24]
            errors=0; inserted=0; batch=[]
            with file.open(encoding='utf-8-sig',newline='') as stream:
                records=(line for line in stream) if file.suffix=='.jsonl' else csv.DictReader(stream)
                for i,raw in enumerate(records,1):
                    if stop and stop.is_set():return
                    try:
                        rec=json.loads(raw) if file.suffix=='.jsonl' else csv_record(raw)
                        if not isinstance(rec,dict) or not isinstance(rec.get('rawHex'),str):raise ValueError()
                        rec.setdefault('kind','packet_log'); batch.append(row_for(rec,session,f'{file.stem}:{i}'))
                    except (ValueError,TypeError,KeyError,OverflowError,AttributeError,RecursionError):errors+=1
                    if len(batch)>=200:
                        inserted+=_import_batch(con,session,file.name,batch);batch=[]
            if batch:inserted+=_import_batch(con,session,file.name,batch)
            with con:con.execute('INSERT OR REPLACE INTO imported_files VALUES(?,?,?,?,?)',(file.name,signature,inserted,errors,utc()))
            if errors:logger.warning('[JOURNAL] %s: %d registros antigos não importados; arquivo original preservado.',file.name,errors)

def reindex_wrapped_history(path,stop=None):
    """Refresh derived columns only; never duplicate or overwrite the original frame."""
    last=0
    names=('type','subtype','retry','satellites','hdop','battery','distance','ordinal','operation','phase','source_profile','target_profile','wire_type','wire_subtype')
    indexes=[_COLUMNS.index(name) for name in names]
    sql='UPDATE packets SET '+','.join(name+'=?' for name in names)+' WHERE id=?'
    with closing(connect(path)) as con:
        while not (stop and stop.is_set()):
            rows=con.execute("SELECT id,session,record FROM packets WHERE kind='packet_log' AND type='CONTROL' AND subtype='METRIC_UPLINK' AND wire_subtype IS NULL AND id>? ORDER BY id LIMIT 128",(last,)).fetchall()
            if not rows:break
            updates=[]
            for row in rows:
                last=row['id']
                try:values=row_for(json.loads(row['record']),row['session'])
                except (ValueError,TypeError,KeyError,OverflowError,AttributeError,RecursionError):continue
                updates.append(tuple(values[i] for i in indexes)+(last,))
            with con:con.executemany(sql,updates)

def _import_batch(con,session,source,batch):
    with con:
        con.execute('INSERT OR IGNORE INTO capture_sessions(id,started,source) VALUES(?,?,?)',(session,min(r[1] for r in batch),source))
        con.execute('UPDATE capture_sessions SET started=min(started,?),ended=max(coalesce(ended,?),?) WHERE id=?',(min(r[1] for r in batch),max(r[1] for r in batch),max(r[1] for r in batch),session))
        before=con.total_changes;con.executemany(_SQL,batch);return con.total_changes-before

class PacketJournal:
    def __init__(self,directory,*,capacity=2048):
        root=Path(directory).expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
        self.db_path=root/'vfence_packets.db';self.session=uuid4().hex;self.started=utc()
        self._queue=queue.Queue(maxsize=capacity);self._closing=threading.Event();self._failed=threading.Event()
        self.written=self.dropped=self.failed_writes=0
        self._thread=threading.Thread(target=self._run,name='vfence-packet-journal',daemon=True);self._thread.start()
    def submit(self,packet):
        if not self._closing.is_set() and not self._failed.is_set():
            try:self._queue.put_nowait({**packet,'hostTimeUtc':utc()});return True
            except queue.Full:pass
        self.dropped+=1
        if self.dropped==1:logger.warning('[JOURNAL] Registro descartado; captura incompleta. Base continua operacional.')
        return False
    def _import(self):
        try:
            reindex_wrapped_history(self.db_path,self._closing)
            import_history(self.db_path,self._closing)
        except (OSError,sqlite3.Error,UnicodeError,ValueError) as exc:logger.warning('[JOURNAL] Importação incompleta (%s); arquivos originais preservados.',type(exc).__name__)
    def _run(self):
        importer=None
        try:
            initialize(self.db_path)
            with closing(connect(self.db_path)) as con:
                with con:con.execute('INSERT INTO capture_sessions(id,started,source) VALUES(?,?,?)',(self.session,self.started,'live'))
                importer=threading.Thread(target=self._import,daemon=True,name='vfence-history-import');importer.start()
                while not self._closing.is_set() or not self._queue.empty():
                    try:record=self._queue.get(timeout=.15)
                    except queue.Empty:continue
                    batch=[record]
                    while len(batch)<128:
                        try:batch.append(self._queue.get_nowait())
                        except queue.Empty:break
                    rows=[]
                    for rec in batch:
                        try:rows.append(row_for(rec,self.session))
                        except (ValueError,TypeError,KeyError,OverflowError,AttributeError,RecursionError):self.failed_writes+=1
                    if len(rows)!=len(batch) and self.failed_writes==len(batch)-len(rows):logger.warning('[JOURNAL] Registro incompatível descartado; Base continua operacional.')
                    try:
                        with con:con.executemany(_SQL,rows)
                        self.written+=len(rows)
                    except sqlite3.Error:
                        self.failed_writes+=len(rows);raise
                    with con:con.execute('UPDATE capture_sessions SET dropped=?,failed=? WHERE id=?',(self.dropped,self.failed_writes,self.session))
                with con:con.execute('UPDATE capture_sessions SET ended=?,dropped=?,failed=? WHERE id=?',(utc(),self.dropped,self.failed_writes,self.session))
        except (OSError,sqlite3.Error,RuntimeError,ValueError) as exc:
            self._failed.set();self.failed_writes+=self._queue.qsize();logger.warning('[JOURNAL] Falha SQLite (%s); Base continua operacional.',type(exc).__name__)
        finally:
            if importer is not None:importer.join(timeout=1)
    def close(self,timeout=5.):
        self._closing.set();self._thread.join(timeout=timeout)
        if self._thread.is_alive() or self.dropped or self.failed_writes:logger.warning('[JOURNAL] Captura possivelmente incompleta: gravados=%d descartados=%d falhas=%d',self.written,self.dropped,self.failed_writes)
