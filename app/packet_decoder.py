"""Read-only VFENCE v1 wire inspector (big endian). Never used for admission."""
TYPES = ['CONFIG', 'EVENT', 'TELEMETRY', 'CONTROL']
SUBTYPES = [ ['FENCE_BEGIN','FENCE_CHUNK','FENCE_COMMIT','PARAMETERS'], ['OUTSIDE','BACK_INSIDE','GPS_LOST','GPS_RECOVERED','LOW_BATTERY','CRITICAL_BATTERY','DEVICE_STARTED'], ['POSITION_STATUS','HDOP_WINDOW_SUMMARY','POSITION_STATUS_COUNTED'], ['ACK','NACK','PING','PONG','PROFILE_CONTROL','METRIC_CLOCK','METRIC_PROOF','METRIC_UPLINK'] ]
PHASES = ['PREPARE','READY','ARM','ARMED','POLL','VERIFIED','RECOVER','RESTORED']
STATES = ['inside','near','veryNear','gpsValid','lowBattery','criticalBattery','buzzerActive','secondActuatorActive']
ERRORS = ['INVALID_LENGTH','UNKNOWN_SUBTYPE','INVALID_COLLAR_ID','UNSUPPORTED_VERSION','INVALID_PARAMETER','INVALID_FENCE_VERSION','INVALID_CHUNK','MISSING_CHUNKS','CRC_ERROR','COMMIT_REJECTED','BUSY','PERSISTENCE_ERROR']

def inspect_packet(raw, *, _allow_envelope=True):
    fields, decoded = [], {}
    result = {'fields':fields,'decoded':decoded,'status':'unknown','reason':''}
    try:
        if not isinstance(raw,str) or len(raw)>8192: raise ValueError()
        b=bytes.fromhex(raw)
    except ValueError:
        result.update(status='invalid',reason='Hexadecimal inválido'); return result
    pos=0
    def add(name,n,fmt='int'):
        nonlocal pos
        start=pos; end=pos+n
        if end>len(b): raise ValueError('Campo truncado')
        part=b[start:end]; pos=end; v=int.from_bytes(part,'big',signed=fmt=='coord')
        if fmt=='hex': v=part.hex().upper()
        elif fmt=='coord': v=None if v==-(2**31) else v/1e7
        elif fmt=='u16': v=None if v==65535 else v
        elif fmt=='u8': v=None if v==255 else v
        elif fmt=='hdop': v=None if v==65535 else v/100
        elif fmt=='flags': v={k:bool(v&(1<<i)) for i,k in enumerate(STATES)}
        elif fmt=='phase': v=PHASES[v-1] if 1<=v<=8 else f'Desconhecido ({v})'
        elif fmt=='error': v=ERRORS[v-1] if 1<=v<=len(ERRORS) else v
        elif fmt=='reset': v=['UNKNOWN','POWER_ON','SOFTWARE','WATCHDOG','BROWNOUT','OTHER'][v] if v<6 else v
        elif fmt=='control': v={'version':v>>6,'type':TYPES[(v>>4)&3],'ackReq':bool(v&8),'retry':bool(v&4),'urgent':bool(v&2),'reserved':bool(v&1)}
        fields.append({'name':name,'start':start,'end':end-1,'hex':part.hex(' ').upper(),'value':v,'section':'header' if start<5 else 'payload'})
        decoded[name]=v; return v
    def coords(prefix=''):
        add(prefix+'latitude',4,'coord');add(prefix+'longitude',4,'coord')
    def tail():
        if pos<len(b): add('bytesNãoInterpretados',len(b)-pos,'hex')
    try:
        if len(b)<5:
            tail(); raise ValueError('Cabeçalho incompleto')
        control=add('controle',1,'control'); add('collarId',1);add('sequence',2); length=add('payloadLength',1)
        if length!=len(b)-5 or len(b)>100 or control['reserved']:
            tail(); raise ValueError('Comprimento ou bit reservado inválido')
        if control['version']!=0:
            tail(); result['reason']='Versão não suportada'; return result
        if not length: raise ValueError('Payload vazio')
        sub=add('subtypeId',1); t=TYPES.index(control['type']); names=SUBTYPES[t]
        if not 1<=sub<=len(names):
            tail(); result['reason']='Subtipo não reconhecido';return result
        name=names[sub-1]; decoded['type']=control['type'];decoded['subtype']=name
        if name!='METRIC_UPLINK' and len(b)>64:
            tail();raise ValueError('Pacote excede limite de 64 bytes')
        if name in ('METRIC_CLOCK','METRIC_PROOF','METRIC_UPLINK'):
            if any(control[k] for k in ('ackReq','retry','urgent')) or add('evidenceVersion',1)!=1:
                tail();raise ValueError('Cabeçalho de evidência inválido')
            if name=='METRIC_UPLINK':
                if not _allow_envelope or not 41<=len(b)<=100:
                    tail();raise ValueError('Encapsulamento inválido')
                gateway=add('evidenceGatewaySession',8,'hex');boot=add('evidenceCollarSession',8,'hex');window=add('evidenceWindowId',8,'hex')
                add('physicalAttemptOrdinal',4);profile=add('evidenceProfileId',1)
                inner_offset=pos # B36: início do quadro VFENCE original
                inner=inspect_packet(b[pos:].hex(),_allow_envelope=False)
                iv=inner['decoded']; ic=iv.get('controle',{})
                if not all(int(x,16) for x in (gateway,boot,window)) or profile>3 or ic.get('version')!=0 or iv.get('collarId')!=decoded['collarId'] or iv.get('sequence')!=decoded['sequence'] or inner['status']=='invalid' or iv.get('subtype')=='METRIC_UPLINK':
                    tail();raise ValueError('Mensagem interna ou identidade inválida')
                for f in inner['fields']:
                    fields.append({**f,'name':'interno.'+f['name'],'start':f['start']+inner_offset,'end':f['end']+inner_offset,'section':'payload'})
                # Inner parser may return early for an unsupported message; keep every byte visible.
                end=inner_offset+sum(f['end']-f['start']+1 for f in inner['fields'])
                pos=end;tail()
                result['envelope']=dict(decoded)
                result['inner']=inner
                if inner['status']=='decoded':
                    result['decoded']={**iv,'wireType':'CONTROL','wireSubtype':'METRIC_UPLINK','physicalAttemptOrdinal':decoded['physicalAttemptOrdinal'],'evidenceProfileId':profile,'evidenceWindowId':window,'evidenceGatewaySession':gateway,'evidenceCollarSession':boot}
                    result['status']='decoded'
                else:result['reason']='Mensagem interna: '+inner['reason']
                return result
            mode=add('evidenceMode',1)
            if name=='METRIC_CLOCK':
                if (mode,len(b)) not in ((1,20),(2,45)):
                    tail();raise ValueError('Comprimento ou modo METRIC_CLOCK inválido')
                add('nonce',4)
                if mode==2:
                    add('evidenceGatewaySession',8,'hex');add('evidenceWindowId',8,'hex');add('windowStartMs',4);add('gatewayTickMs',4);add('evidenceProfileId',1)
                add('evidenceCollarSession',8,'hex')
            else:
                if (mode,len(b)) not in ((1,48),(2,32)):
                    tail();raise ValueError('Comprimento ou modo METRIC_PROOF inválido')
                add('evidenceGatewaySession',8,'hex');add('evidenceCollarSession',8,'hex');add('evidenceWindowId',8,'hex')
                if mode==1:
                    add('windowStartMs',4);add('firstTx',4);add('lastTx',4);add('hdopMean',2,'hdop');add('sampleCount',1);add('evidenceProfileId',1)
            result['status']='decoded';return result
        expected={'FENCE_BEGIN':14,'FENCE_COMMIT':12,'PARAMETERS':18,'OUTSIDE':22,'BACK_INSIDE':22,'GPS_LOST':16,'GPS_RECOVERED':17,'LOW_BATTERY':17,'CRITICAL_BATTERY':17,'DEVICE_STARTED':10,'POSITION_STATUS':23,'POSITION_STATUS_COUNTED':36,'HDOP_WINDOW_SUMMARY':32,'ACK':9,'NACK':11,'PING':8,'PONG':8,'PROFILE_CONTROL':32}
        if name=='FENCE_CHUNK':
            if len(b)<11 or b[10] not in (1,2) or len(b)!=11+8*b[10]: tail(); raise ValueError('Chunk com comprimento inválido')
        elif len(b)!=expected[name]: tail(); raise ValueError('Comprimento incompatível com subtipo')
        if name in ('FENCE_BEGIN','FENCE_CHUNK','FENCE_COMMIT'):
            add('fenceVersion',2)
            if name=='FENCE_BEGIN': add('vertexCount',1);add('chunkCount',1);add('crc32',4,'hex')
            elif name=='FENCE_COMMIT': add('crc32',4,'hex')
            else:
                add('chunkIndex',1);add('firstVertex',1);count=add('pointCount',1)
                for i in range(count): coords(f'v{i+1}.')
        elif name=='PARAMETERS':
            for k,n in [('veryNearM',2),('nearM',2),('telemetryIntervalS',4),('gpsTimeoutS',2),('lowBatteryPct',1),('criticalBatteryPct',1)]:add(k,n)
        elif name in ('OUTSIDE','BACK_INSIDE','POSITION_STATUS','POSITION_STATUS_COUNTED'):
            if t==1: add('fenceVersion',2)
            coords();add('distanceBorderM',2,'u16');add('batteryPct',1,'u8');add('satellites',1,'u8');add('hdop',2,'hdop')
            if t==2: add('stateFlags',1,'flags');add('fenceVersion',2)
            if name=='POSITION_STATUS_COUNTED':add('collarSessionId',8,'hex');add('positionOrdinal',4);add('packetProfileId',1)
        elif name=='GPS_LOST':coords('last.');add('positionAgeS',2,'u16')
        elif name=='GPS_RECOVERED':coords();add('satellites',1,'u8');add('hdop',2,'hdop')
        elif name in ('LOW_BATTERY','CRITICAL_BATTERY'):add('batteryPct',1,'u8');coords('last.');add('positionAgeS',2,'u16')
        elif name=='DEVICE_STARTED':add('resetReason',1,'reset');add('fenceVersion',2);add('batteryPct',1,'u8')
        elif name=='HDOP_WINDOW_SUMMARY':
            add('collarSessionId',8,'hex');add('windowId',8,'hex');add('startedAtMs',4);add('durationMs',2);add('hdopMean',2,'hdop');add('sampleCount',1);add('hdopMeanValid',1)
        elif name=='ACK':add('ackedSequence',2);add('status',1)
        elif name=='NACK':add('rejectedSequence',2);add('errorCode',1,'error');add('detail',2)
        elif name in ('PING','PONG'):add('token',2)
        elif name=='PROFILE_CONTROL':
            if add('profileControlVersion',1)!=2:tail();result['reason']='Versão interna não suportada';return result
            add('profileStep',1,'phase');add('profileSource',1);add('profileTarget',1);add('profileOperationId',4);add('gatewaySessionId',8,'hex');add('collarSessionId',8,'hex');add('profileWaitMs',2)
        result['status']='decoded'
    except ValueError as exc:
        result.update(status='invalid',reason=str(exc))
    return result
