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
- Protocolo Serial estruturado `vfence.gateway.serial.v1` para a Base Python
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

Baud: 115200. Termine cada comando com LF ou CRLF.

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

## Protocolo Serial da Base

A integracao automatica usa uma mensagem JSON compacta por linha, a 115200 baud.
A Base envia o prefixo `[CMD] ` e o gateway responde ou publica eventos com
`[APP] `. O schema atual e `vfence.gateway.serial.v1`.

Exemplo de comando:

```text
[CMD] {"schema":"vfence.gateway.serial.v1","requestId":1,"command":"GET_STATUS"}
```

Resposta correlacionada:

```text
[APP] {"schema":"vfence.gateway.serial.v1","kind":"command_response","requestId":1,"command":"GET_STATUS","ok":true}
```

Comandos estruturados suportados:

- `GET_STATUS`
- `GET_RADIO`
- `PING`
- `SET_PARAMETERS`
- `FENCE_NEW`
- `FENCE_POINT`
- `FENCE_SEND`
- `FENCE_CANCEL`

Mensagens publicadas pelo gateway usam os tipos `telemetry`, `event`,
`control`, `collar_state`, `collar_status`, `gateway_state`, `radio_config`,
`command_response` e `fence_transfer`.

O `requestId` de `FENCE_SEND` acompanha toda a transferencia. O gateway emite
`fence_transfer` com estado `started` ao iniciar, `confirmed` apos o ACK do
`FENCE_COMMIT`, ou `failed` quando a maquina de estados LoRa encerra com erro.

```text
[APP] {"schema":"vfence.gateway.serial.v1","kind":"fence_transfer","requestId":8,"collarId":1,"fenceVersion":3,"state":"confirmed"}
```

O Gateway recebe até 512 bytes de comando, incluindo o prefixo [CMD] e
excluindo LF ou CRLF. Para comandos estruturados, valida um único objeto JSON
antes de interpretar os campos de comando na raiz. As chaves usadas pelo
despachante não podem aparecer duas vezes. Os comandos textuais usam as mesmas
funções de envio LoRa.

No primeiro envio de cada conexão USB, e após detectar uma escrita incompleta,
a Base antepõe os bytes 0x00 0x0A. O Gateway usa NUL para invalidar o fragmento
anterior e LF para terminar o descarte. O marcador pode ser enviado na mesma
escrita do próximo comando e fica fora do JSON e do seu limite de tamanho.

## Resumos de métricas e confirmação de persistência

Em bancada, a 115200 baud, cada resumo ocupa uma linha JSON compacta com prefixo
`[APP] ` e terminador LF. O total, inclusive prefixo e LF, cabe em 512 bytes.

```text
[APP] {"schema":"vfence.gateway.serial.v1","kind":"hdop_window_summary","collarId":1,"collarSessionId":"000000000000000a","windowId":"0000000000000001","collarStartedAtMs":0,"durationMs":60000,"hdopMeanX100":142,"sampleCount":58,"hdopMeanValid":true,"gatewaySessionId":"000000000000000b","gatewayReceivedAtMs":61250}
[APP] {"schema":"vfence.gateway.serial.v1","kind":"radio_window_summary","collarId":1,"gatewaySessionId":"000000000000000b","windowId":"0000000000000001","gatewayStartedAtMs":1200,"durationMs":60000,"rssiMeanX100":-9340,"snrMeanX100":-375,"sampleCount":8,"rssiMeanValid":true,"snrMeanValid":true}
```

Os identificadores têm 16 dígitos hexadecimais minúsculos, inclusive zeros à
esquerda. As médias usam escala ×100 e valem `null` em janelas vazias. Carimbos
`*AtMs` são relativos aos respectivos produtores. Uma janela com o mesmo ID dos
dois lados pode representar intervalos diferentes.

Após `INSERTED` ou `DUPLICATE` com COMMIT concluído, a Base emite o formato
canônico a seguir. A ordem e a grafia dos campos, a ausência de espaços dentro
do JSON e o LF final fazem parte desse formato.

```text
[CMD] {"schema":"vfence.gateway.serial.v1","requestId":17,"command":"METRIC_COMMITTED","kind":"hdop_window_summary","collarId":1,"sessionId":"000000000000000a","windowId":"0000000000000001","gatewaySessionId":"000000000000000b"}
```

`requestId` é um inteiro de 1 a 4294967295. `kind` é o mesmo do resumo.
`sessionId` vem de `collarSessionId` para HDOP e de `gatewaySessionId` para
RADIO. O último campo identifica o Gateway que enviou o resumo. O comando é
unidirecional, sem `command_response`. O Gateway consome somente a cabeça de
fila que corresponde ao tipo, à coleira, à sessão, à janela e ao envio pendente.

O Gateway alterna tipos e IDs a cada novo turno. A repetição fica elegível
após 1,5 s e o turno vence 4,5 s após o primeiro envio aceito. Se uma escrita
for recusada localmente, as próximas tentativas aguardam pelo menos 500 ms.
Cada turno permite no máximo uma repetição aceita. Ao vencer o prazo, a janela
continua disponível para nova tentativa enquanto estiver na RAM. Cercas, rádio,
controle e atividade Serial têm precedência. A capacidade das filas e a política
de sobrescrita existentes continuam valendo. Reiniciar o Gateway perde a RAM.
O trecho LoRa do HDOP continua sem confirmação de recepção.

### Ativação em bancada

1. Na Base, configure `METRICS_INSTALLATION_ID` e `METRICS_GATEWAY_ID` com
   identificadores estáveis da instalação e do Gateway. Escolha os valores de
   acordo com o provisionamento local. Um ID ausente desabilita confirmação e
   gravação do tipo correspondente.
2. Na Base, defina `METRICS_SERIAL_ACK_ENABLED=true`, use Serial real a
   115200 baud e reinicie o processo.
3. Para um ensaio controlado, altere `METRIC_SERIAL_EXPORT_ENABLED` no
   `gateway_config.h`, compile e grave o Gateway. A emissão depende de um core
   Arduino ESP32 com buffer UART TX configurável e espaço reportado por
   `availableForWrite()`. A configuração de fábrica mantém as duas flags desligadas.
4. Confira `metric_summaries` em um SQLite de bancada. Interrompa a USB,
   confirme que os reenvios preservam a identidade e confira a quantidade de
   linhas após reconectar. Teste cercas e posições com o tráfego concorrente.

Antes da ativação operacional contínua, verifique a recusa de alertas e estados
sob saturação da fila da Base, a contenção de consultas WebSocket antes da
inscrição e a lacuna de atualização de cerca entre snapshot e inscrição. Valide
também o transporte e a vazão em hardware.
