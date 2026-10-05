# VFenceGateway - Gateway beta do VFENCE Protocol v1

Firmware separado do firmware da coleira. O gateway nao executa GPS, geofence nem atuadores. Ele compartilha apenas `vfence_protocol.h/.cpp` com a coleira.

## Funcoes implementadas

- Recepcao LoRa real no Heltec WiFi LoRa 32 (V2)
- Decodificacao de `TELEMETRY / POSITION_STATUS`
- Decodificacao de todos os `EVENT` definidos na v1
- ACK automatico para eventos confiaveis
- Deteccao de duplicatas de EVENT por `sequence`
- `DEVICE_STARTED` inicia nova sessao quando chega sem flag RETRY
- PING/PONG
- Tratamento de ACK/NACK recebidos
- Fila de mensagens confiaveis Gateway -> Coleira com retry
- Comando `PARAM` para testar configuracao remota
- Registro de RSSI e SNR no gateway
- `lastSeen` e deteccao de `COLLAR_OFFLINE` apos 180 s
- Saida humana no Monitor Serial
- Saida `[APP] {...}` em JSON por linha, preparando futura ponte para a aplicacao
- Injecao `RX <hex...>` para testar decodificacao sem usar radio

## Arquivos

- `VFenceGateway.ino`: firmware principal do gateway
- `gateway_config.h`: pinos e parametros LoRa
- `vfence_protocol.h/.cpp`: mesma implementacao binaria usada pela coleira

## Biblioteca necessaria

No Arduino IDE, instale pelo Library Manager:

- **LoRa by Sandeep Mistry**

`SPI` ja faz parte do ambiente Arduino/ESP32.

## Placa

Selecione a mesma familia de placa usada no projeto:

`Heltec WiFi LoRa 32(V2)`

## Pinos LoRa usados no Heltec V2

- SCK = GPIO 5
- MISO = GPIO 19
- MOSI = GPIO 27
- NSS/SS = GPIO 18
- RESET = GPIO 14
- DIO0 = GPIO 26

O GPS nao e ligado ao gateway.

## Configuracao de radio inicial

- Frequencia: 915 MHz
- SF: 7
- BW: 125 kHz
- CR: 4/5
- Preambulo: 8 simbolos
- Sync word: 0x12
- CRC LoRa: habilitado
- Potencia TX: 14 dBm

**Gateway e coleira precisam usar exatamente os mesmos parametros de radio.**

Esses valores sao o ponto de partida da prova de conceito; o tuning de SF/BW/CR sera feito depois com medidas de RSSI, SNR, perda de pacotes e airtime.

## Comandos no Monitor Serial

Baud: 115200.

### Ver ajuda

`HELP`

### Ver coleiras vistas pelo gateway

`STATUS`

### Ver configuracao LoRa

`RADIO`

### PING

`PING 1`

ou

`PING 1 1234`

### Alterar parametros da coleira 1

Formato:

`PARAM <id> <muitoPertoM> <pertoM> <telemetryS> <gpsTimeoutS> <lowBat> <critBat>`

Exemplo:

`PARAM 1 10 30 10 60 20 5`

A mensagem PARAMETERS exige ACK e entra automaticamente na logica de retry.

### Testar um pacote sem LoRa

Cole uma mensagem hexadecimal apos `RX`:

`RX 20 01 00 05 12 01 ED 55 F9 F7 DF C3 03 07 00 03 FF 0C 00 6E CD 00 01`

O gateway deve decodificar a telemetria e mostrar os campos no Monitor Serial.

## Importante: estado atual da integracao

O gateway deste projeto ja usa LoRa real. A versao atual do firmware da coleira entregue anteriormente ainda tinha `transportSend()` imprimindo o pacote na Serial. Portanto, para testar **Heltec <-> Heltec pelo radio**, o proximo ajuste e substituir o transporte simulado da coleira pelo mesmo driver LoRa/configuracao de radio.

## Transferencia manual de cerca pelo Monitor Serial

O gateway tambem implementa o fluxo completo `FENCE_BEGIN -> FENCE_CHUNK -> FENCE_COMMIT`, incluindo ACK do BEGIN/COMMIT e reenvio seletivo de chunks quando a coleira responder `NACK MISSING_CHUNKS`.

Exemplo com uma cerca de 4 vertices para a coleira 1, versao 2:

```text
FENCE_NEW 1 2 4
FENCE_POINT 0 -31.3133945 -54.0868848
FENCE_POINT 1 -31.3131703 -54.0868848
FENCE_POINT 2 -31.3131703 -54.0867535
FENCE_POINT 3 -31.3133945 -54.0867535
FENCE_SHOW
FENCE_SEND
```

O gateway calcula o CRC32, divide automaticamente em 2 vertices por chunk e espera os ACKs necessarios. Se o `FENCE_COMMIT` retornar um bitmap de chunks ausentes, apenas os chunks faltantes sao reenviados e um novo COMMIT e enviado.

`FENCE_CANCEL` cancela uma transferencia ativa. Uma nova chamada `FENCE_NEW` substitui uma cerca preparada quando nao existe transferencia em andamento.
