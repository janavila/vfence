import asyncio
import csv
import io
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import protocol_monitor as monitor
from app.packet_decoder import inspect_packet
from app.packet_journal import PacketJournal, import_history, initialize, _PACKET_CSV_FIELDS
from app.database import Database
from app.serial_reader import SerialReader


def position():
    raw=bytes.fromhex('2002008E1F03')+struct.pack('>iiHBBHBH',-313131090,-540864230,25,87,9,124,9,10)+struct.pack('>QIB',42,142,0)
    assert len(raw)==36
    return {'schema':'vfence.gateway.serial.v1','kind':'packet_log','gatewaySessionId':'0123456789abcdef','gatewayMs':10,'direction':'RX','valid':True,'collarId':2,'type':'TELEMETRY','subtype':'POSITION_STATUS_COUNTED','sequence':142,'profileId':0,'rssiDbm':-87,'snrDb':8.2,'retry':False,'rawHex':raw.hex()}


def profile(phase=1, direction='TX'):
    raw=bytes.fromhex('3002000A1B0502')+bytes([phase,0,3])+struct.pack('>IQQH',1,0x0123456789abcdef,42,0)
    return {'schema':'vfence.gateway.serial.v1','kind':'packet_log','gatewaySessionId':'0123456789abcdef','direction':direction,'valid':True,'type':'CONTROL','subtype':'PROFILE_CONTROL','collarId':2,'profileId':0 if phase<5 else 3,'sequence':10,'rawHex':raw.hex()}


def pdr():
    return {'schema':'vfence.gateway.pdr.v1','kind':'pdr_window_summary','collarId':2,'gatewaySessionId':'0123456789abcdef','windowId':'0000000000000001','collarSessionId':'000000000000002a','startedAtMs':0,'durationMs':60000,'profileId':0,'receivedCount':2,'estimatedTxCount':3,'estimatedLostCount':1,'pdrX100':6667,'valid':True,'reason':'ok'}


def ack():
    return {**profile(),'direction':'RX','subtype':'ACK','rawHex':'320200900401008400','sequence':144}


def test_decode_wire():
    d=inspect_packet(position()['rawHex']);assert d['status']=='decoded';v=d['decoded'];assert v['satellites']==9 and v['hdop']==1.24 and v['latitude']==-31.313109 and v['positionOrdinal']==142 and v['distanceBorderM']==25
    assert ''.join(f['hex'].replace(' ','') for f in d['fields'])==position()['rawHex'].upper()
    p=inspect_packet(profile(6)['rawHex']);assert p['decoded']['profileStep']=='VERIFIED';assert p['decoded']['gatewaySessionId']=='0123456789ABCDEF'
    assert inspect_packet(ack()['rawHex'])['decoded']['ackedSequence']==132
    assert inspect_packet('200201011F03')['status']=='invalid'
    assert inspect_packet('2002000101FF')['status']=='unknown'
    assert inspect_packet('oops')['status']=='invalid'


def test_all_known_lengths():
    # The layouts match encode functions in vfence_protocol.cpp plus shared extensions.
    for t, lengths in enumerate([[9,14,7,13],[17,17,11,12,12,12,5],[18,27,31],[4,6,3,3,27]]):
        for sub,length in enumerate(lengths,1):
            payload=bytearray(length);payload[0]=sub
            if t==0 and sub==2:payload[5]=1
            if t==3 and sub==5:payload[1:5]=bytes([2,1,0,3])
            raw=bytes([t<<4,2,0,1,length])+payload
            d=inspect_packet(raw.hex());assert d['status']=='decoded',(t,sub,d)
            assert ''.join(f['hex'].replace(' ','') for f in d['fields'])==raw.hex().upper()


def test_persistent_import_and_api(tmp_path,monkeypatch):
    for _ in range(2):
        journal=PacketJournal(tmp_path)
        for rec in (position(),ack(),profile(),profile(6,'RX'),pdr()):assert journal.submit(rec)
        journal.close();assert journal.written==5 and not journal.failed_writes
    db=tmp_path/'vfence_packets.db';assert db.exists();assert not list(tmp_path.glob('*.csv'))
    rec={**position(),'hostTimeUtc':'2026-11-27T22:00:00.000+00:00'}
    f=tmp_path/'vfence_packets_old.jsonl';f.write_text(json.dumps(rec)+'\n')
    companion=f.with_suffix('.csv');companion.write_text('ignored companion')
    old=tmp_path/'vfence_packets_csvonly.csv'
    with old.open('w',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=_PACKET_CSV_FIELDS,extrasaction='ignore');w.writeheader();w.writerow(rec)
    import_history(db);import_history(db)
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT count(*) FROM packets').fetchone()[0]==12
        assert c.execute('SELECT count(*) FROM capture_sessions').fetchone()[0]==4
    monkeypatch.setattr(monitor,'DB_PATH',db)
    app=FastAPI();app.include_router(monitor.router)
    client=TestClient(app,client=('127.0.0.1',50000))
    assert client.get('/monitor').status_code==200
    assert len(client.get('/api/protocol-monitor/packets?category=TELEMETRY').json()['rows'])==4
    assert len(client.get('/api/protocol-monitor/packets?category=confirm').json()['rows'])==2
    assert len(client.get('/api/protocol-monitor/packets?category=profile&profile=3').json()['rows'])==2
    assert client.get('/api/protocol-monitor/compare').json()['total']['packets']==10
    rows=client.get('/api/protocol-monitor/packets?category=TELEMETRY').json()['rows'];assert rows[0]['satellites']==9
    r=client.get('/api/protocol-monitor/packets/'+str(rows[0]['id'])).json();assert r['inspection']['decoded']['positionOrdinal']==142
    m=client.get('/api/protocol-monitor/metrics?collar=2').json();assert len(m['summaries'])==2 and m['summaries'][0]['pdr']==66.67 and len(m['marks'])==2
    assert len(client.get('/api/protocol-monitor/transitions').json()['groups'])==1
    exported=client.get('/api/protocol-monitor/export?category=confirm');assert len(list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff')))))==2
    assert TestClient(app,client=('192.0.2.10',50000)).get('/monitor').status_code==403


def test_serial_capture_silent(tmp_path):
    class Hub:
        async def broadcast(self,msg):raise AssertionError('packet journal should not broadcast')
    cfg=SimpleNamespace(mock_serial=False,windows_base=True,terminal_detail=True,serial_port='COM3',max_line_length=512,central_sync_enabled=False)
    db=Database('sqlite:///:memory:');db.initialize();reader=SerialReader(cfg,db,Hub());journal=PacketJournal(tmp_path);reader._packet_journal=journal
    async def exercise():
        with patch('app.serial_reader.terminal_emit') as display,patch.object(reader,'_process_gateway_app',new_callable=AsyncMock,return_value=True) as dispatch:
            wire=lambda r:('[APP] '+json.dumps(r)+'\r\n').encode()
            await reader._replay_startup_serial(wire(position()))
            buf=wire(profile());await reader._process_serial_chunk(buf[:90]);await reader._process_serial_chunk(buf[90:])
            assert not display.called and not dispatch.called
            await reader.process_line(wire(pdr()))
            assert dispatch.await_count==1
    asyncio.run(exercise());journal.close();assert journal.written==3
    assert reader.collar_contacts=={}
