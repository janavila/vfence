# VFENCE Protocol v1 — implementação beta

Este projeto separa a aplicação VFENCE em três camadas principais:

1. **`vfence_protocol.*`** — wire format binário, big-endian, serialização, desserialização e CRC32.
2. **`vfence_fence_transfer.*`** — montagem de cercas fragmentadas, bitmap de chunks, validação e commit.
3. **`vfence_geofence.*`** — Point-in-Polygon, distância até a borda e estados FAR / NEAR / VERY_NEAR.
4. **`VFenceProtocolV1.ino`** — integra GPS NEO-6M, geofence, eventos, telemetria, ACK/retry, persistência e atuadores.

## Importante: LoRa ainda está desacoplado

Nesta etapa, `transportSend()` apenas imprime os bytes no Monitor Serial. Isso é proposital: primeiro validamos o protocolo e a lógica da coleira sem misturar erros de rádio com erros do wire format.

Quando a camada LoRa for ligada, o ponto principal de integração será `transportSend()` e uma função equivalente para receber bytes do rádio e chamar:

```cpp
processIncomingPacket(buffer, len);
```

## Dependências Arduino

- ESP32 / Heltec WiFi LoRa 32 (V2)
- TinyGPSPlus
- Preferences (já faz parte do core ESP32)

GPS usado no projeto:

- NEO-6M
- TX do GPS -> GPIO 23 do ESP32
- baud identificado no módulo: 115200

## Parâmetros beta

- `MAX_VERTICES = 32`
- 2 vértices por `FENCE_CHUNK`
- GPS_LOST após 60 s sem posição válida
- telemetria padrão a cada 10 s + jitter 0–3 s
- ACK timeout 5 s
- jitter de retry 0–2 s
- 4 transmissões no máximo (1 original + 3 retries)

## Teste sem LoRa

Os pacotes transmitidos aparecem assim:

```text
[TX 23 bytes] 20 01 00 01 12 ...
```

Também é possível injetar um pacote recebido pelo Monitor Serial:

```text
RX <bytes hex separados por espaço>
```

A linha é convertida em bytes e passada para `processIncomingPacket()`.

## Testes de unidade

O diretório `tests/` contém um teste C++ que valida:

- CRC-32/ISO-HDLC usando o vetor padrão `123456789 -> 0xCBF43926`
- leitura/escrita big-endian
- round-trip de TELEMETRY
- montagem e commit de cerca
- detecção dentro/fora
- tamanho e decodificação de FENCE_CHUNK

Esses testes foram executados antes da geração deste pacote e passaram.

## Próximo passo

Integrar o SX127x/LoRa da Heltec sem alterar a lógica de protocolo:

- configurar frequência, SF, BW, CR e potência
- implementar TX/RX real em `transportSend()` / polling do rádio
- medir RSSI, SNR, perda de pacotes e airtime
- testar ACK/retry com dois ESP32 reais
