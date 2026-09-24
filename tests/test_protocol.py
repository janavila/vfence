from datetime import datetime, timezone
import pytest
from app.protocol import ProtocolError, parse_message

def test_legacy_packet():
    packet=parse_message("Recebido: VFENCE_TESTE_630 | RSSI: -103 dBm | SNR: 6.50")
    assert (packet.sequence,packet.rssi,packet.snr,packet.collar_id)==(630,-103,6.5,"LEGACY")

def test_v1_packet():
    now=datetime.now(timezone.utc);packet=parse_message("V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|630",timestamp=now)
    assert packet.latitude==-31.306119 and packet.longitude==-54.063935
    assert packet.zone=="SEGURO" and packet.sequence==630 and packet.timestamp==now

def test_v1_packet_wrapped_by_current_receiver():
    packet=parse_message("Recebido: V1|COL01|POS|-31.3|-54.0|SEGURO|12|0.90|9 | RSSI: -103 dBm | SNR: 6.50")
    assert packet.collar_id=="COL01" and packet.sequence==9
    assert packet.rssi==-103 and packet.snr==6.5

@pytest.mark.parametrize("message",["lixo","V1|COL01|POS|-31|-54|SEGURO|12|0.9","V1|COL01|POS|91|-54|SEGURO|12|0.9|1","V1|COL01|POS|-31|181|SEGURO|12|0.9|1","V1|COL01|POS|-31|-54|DESCONHECIDA|12|0.9|1","V1|COL01|POS|-31|-54|SEGURO|12|0.9|-1","V1|COL01|POS|-31|-54|SEGURO|12|nan|1"])
def test_invalid_packets(message):
    with pytest.raises(ProtocolError): parse_message(message)

def test_empty_line_is_ignored(): assert parse_message(" \r\n") is None
