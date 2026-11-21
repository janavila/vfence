# VFence 5.8 — PDR de posições e perfis globais

## Evidência disponível

O cabeçalho LoRa existente tem sequência de 16 bits compartilhada entre posição, HDOP, eventos e controle. O contador começa novamente no boot e transborda módulo 65.536. Um retry de evento reutiliza a sequência. O emissor também pode alocar uma sequência e falhar antes de concluir o TX. O Gateway pode identificar quadros entregues e algumas duplicatas, mas uma lacuna entre posições legadas não revela quantas posições foram transmitidas. O intervalo configurável e o jitter também impedem deduzir o denominador pelo relógio. O pacote de 23 bytes não carrega a sessão da coleira.

## Extensão opt-in antes de alterar o firmware

O subtipo `0x03 POSITION_STATUS_COUNTED` é adicional. O payload mantém a posição do subtipo existente nos primeiros 18 bytes, com o novo identificador no byte 0, e acrescenta sessão de boot de 64 bits, ordinal de posição de 32 bits, ambos big-endian, e ID do perfil em um byte. Total de 36 bytes. O ordinal considera apenas posições cuja transmissão local retornou sucesso e pode dar wrap. O ordinal enviado é o candidato à próxima transmissão aceita. Os 23 bytes de POSITION_STATUS e os 32 bytes de HDOP_WINDOW_SUMMARY permanecem no protocolo. Para usar o subtipo novo, atualizar o Gateway primeiro. A coleira usa o subtipo antigo por padrão e, na bancada atualizada, passa a emitir o novo no lugar da posição antiga, na mesma cadência, sem transmissão duplicada.

O Gateway usa janelas de recepção de 60 segundos. A estimativa cobre do primeiro ao último ordinal avançado **observado na mesma janela**, considerando somente uma sessão e um perfil. A primeira recepção ancora a janela, duas posições são o mínimo, e a contagem usa ordinais únicos. As lacunas internas são perdas inferidas. A entrega tardia pode preencher lacunas na janela aberta, sem reabrir uma janela publicada. Ordinais anteriores à âncora são ignorados. Wrap usa aritmética modular. Lacuna impossível para a janela, perda de referência, troca de sessão e troca de perfil invalidam a janela afetada. Após silêncio prolongado, o próximo quadro pode restabelecer a referência. Sem amostras ou com uma só, não há PDR. Com 10, 12 e 11, resultado 3/3. Com 10 e 12, resultado 2/3. Posições com GNSS inválido contam. Eventos, controle, HDOP e posição legada ficam fora do PDR. A medida não cobre perdas anteriores e posteriores às âncoras, janelas inteiramente perdidas, TX que não foi aceito localmente, nem eventos confiáveis. A sessão de 64 bits é aleatória e tem probabilidade residual de colisão.

## Contrato novo Gateway → Base

Linha ASCII com prefixo `[APP] `, LF ou CRLF, limite de 512 bytes incluindo terminador. Schema `vfence.gateway.pdr.v1`, kind `pdr_window_summary`. Campos obrigatórios: `collarId` (1–254), `gatewaySessionId`, `windowId` (não zero), `collarSessionId` (nulo quando indeterminado), `startedAtMs`, `durationMs` (60000), `profileId` (0–3 ou nulo), `receivedCount`, `estimatedTxCount`, `estimatedLostCount`, `pdrX100`, `valid`, `reason`. IDs de sessão e janela são 16 dígitos hexadecimais minúsculos. As três quantidades estimadas são nulas nas janelas inválidas. Nas válidas, `estimatedLostCount = estimatedTxCount - receivedCount` e `pdrX100 = round(10000 × receivedCount / estimatedTxCount)` com empate para cima. A Base vincula o dado ao `METRICS_GATEWAY_ID`.

Esse canal de diagnóstico tem retenção limitada à janela mais recente por ID no Gateway e entrega Serial de melhor esforço. Após janela vazia de uma coleira já observada, novos resumos vazios consecutivos são omitidos até a próxima atividade. A Base realiza INSERT idempotente por Gateway, sessão do Gateway, janela e coleira. Conteúdo diferente para a mesma identidade é recusado. O processamento tem prioridade inferior à de HDOP/RADIO, com cota de oito itens em espera. Os contratos de HDOP, RADIO e METRIC_COMMITTED continuam independentes. A exportação PDR na UART é opt-in e usa espaço de TX verificado antes de escrever o quadro inteiro. Em caso de escrita parcial, tenta-se terminar a linha rejeitável e a recusa é contada. Reinício do Gateway, sobrescrita ou desconexão podem perder resumos ainda ausentes do SQLite.

`GET /api/collars/{collar_id}/pdr?limit=60` aceita IDs numéricos de 1 a 254 e limite de 1 a 120. Retorna as janelas gravadas mais recentes com `persisted_at` em UTC. O SQLite cria `pdr_windows` ao inicializar. A atualização de bancada exige atualizar o Gateway primeiro, provisionar o `METRICS_GATEWAY_ID` já existente e habilitar `COUNTED_POSITION_ENABLED` na coleira e `PDR_SERIAL_EXPORT_ENABLED` no Gateway conforme o plano de ensaio. Essas duas opções vêm desativadas. Bases antigas podem rejeitar o novo schema, por isso a exportação deve ficar desativada até a atualização da Base.

## Perfis globais

P0 continua sendo a única configuração aplicada por Gateway e coleiras. P1 a P3 são candidatos para ensaio. Todas as entradas mantêm a frequência nominal de 915 MHz, BW 125 kHz, CR 4/5, 14 dBm solicitados, preâmbulo de oito símbolos, sync word 0x12 e CRC ativo. O cálculo supõe header explícito, IQ normal e DE=0 para SF7–SF10 em BW125. A escolha de variar apenas SF facilita comparar sensibilidade, colisões, ocupação e consumo na mesma configuração de antena. O código de rádio inicial mantém as chamadas originais e os defaults de header e IQ da biblioteca.

| ID | SF | ACK 9 B | Posição 23 B | HDOP 32 B | Posição contada 36 B | Quadro 64 B |
|---|---:|---:|---:|---:|---:|---:|
| P0 | 7 | 41,22 ms | 61,70 ms | 71,94 ms | 77,06 ms | 118,02 ms |
| P1 | 8 | 72,19 ms | 113,15 ms | 133,63 ms | 143,87 ms | 215,55 ms |
| P2 | 9 | 144,38 ms | 205,82 ms | 246,78 ms | 267,26 ms | 390,14 ms |
| P3 | 10 | 247,81 ms | 370,69 ms | 452,61 ms | 493,57 ms | 698,37 ms |

Tempo no ar estimado como `Tsymbol = 2^SF / BW`, com preâmbulo de `8 + 4,25` símbolos, mais os símbolos de payload e CRC, conforme o modem LoRa. SF maior tende a melhorar a capacidade de decodificar SNR mais baixo e aumenta o tempo de transmissão. Em 50 coleiras com posição contada a cada 11,5 s, a ocupação oferecida de um único canal, só por posição, é aproximadamente 33%, 63%, 116% e 215%. P2/P3 ficam acima da capacidade de um canal nessa carga mesmo sem ACK, eventos, HDOP ou cercas. P1 também exige avaliação de colisões. A validação deve medir airtime, tempos de FENCE_CHUNK e ACK/NACK, consumo, capacidade do receptor e margem de enlace.

915 MHz permanece o valor do projeto para compatibilidade. O Ato Anatel nº 14.448 distingue categorias e métodos de acesso. Seu item 10.3.1 exige pelo menos 500 kHz medidos a 6 dB no enquadramento ali descrito. Um canal fixo com BW nominal de 125 kHz exige verificar o enquadramento efetivo em vez de assumir que atende a esse item. Antes de operar em campo, conferir a homologação da variante da placa, a máscara de emissão na borda de 915 MHz, antena, potência/EIRP e canalização.

Fontes técnicas. [Heltec WiFi LoRa 32 V2](https://heltec.org/project/wifi-lora-32v2/) · [Semtech SX1276](https://www.semtech.com/products/wireless-rf/lora-connect/sx1276) · [Semtech sobre SF e tempo no ar](https://www.semtech.com/uploads/technology/LoRa/lora-and-lorawan.pdf) · [API Arduino-LoRa](https://github.com/sandeepmistry/arduino-LoRa/blob/master/API.md) · [Ato Anatel nº 14.448](https://informacoes.anatel.gov.br/legislacao/atos-de-certificacao-de-produtos/2017/1139-ato-14448)

## Interface de controle

Os comandos usam o schema e o enquadramento Serial já existentes, com `requestId` para correlacionar `command_response`. Somente as respostas desses comandos novos têm `profileControlVersion: 1`. `activeProfileId=0` e `switchEnabled=false` descrevem a implementação embarcada atual.

| Comando | Campos adicionais | Ação |
|---|---|---|
| `GET_PROFILE` | — | Consulta perfil ativo, sessão, estado e operação. |
| `LIST_PROFILES` | — | Inclui parâmetros dos quatro perfis e indicação de candidato. |
| `GET_PROFILE_TRANSITION` | — | Consulta o estado do plano em RAM. |
| `REQUEST_PROFILE` | `gatewaySessionId`, `operationId`, `profileId` | Registra uma intenção ou responde que P0 já está ativo. |
| `CANCEL_PROFILE` | `gatewaySessionId`, `operationId` | Cancela a intenção correspondente. |

Use `operationId` positivo e crescente dentro da sessão do Gateway, separado de `requestId`. Um comando que repete o mesmo ID com o mesmo alvo é idempotente. Um ID mais antigo, o mesmo ID com outro alvo, uma sessão divergente, um perfil inexistente e uma segunda intenção concorrente são recusados. O Gateway também recusa novos planos durante transferência ativa de cerca ou enquanto houver TX confiável pendente. Ao reiniciar, gera nova sessão e começa em P0 com o plano vazio. Um plano registrado significa `PLANNED`; não há COMMIT pelo rádio. O Raspberry pode consultar `GET /api/gateway/profile` para obter uma resposta da porta ativa. Essa rota não realiza seleção de perfil nem simula sucesso em modo mock.

## Coordenação futura e critérios de recuperação

O simulador em host exercita as fases abaixo. O firmware executa apenas `IDLE` e `PLANNED`. O protocolo LoRa de transição, o registro durável da operação e a renovação dos prazos ainda precisam ser implementados e validados juntos.

1. **Preparar no perfil antigo.** Fixar uma lista confiável das coleiras operacionais, com ID e sessão de boot. Uma coleira offline ou ainda desconhecida bloqueia a transição. A lista só exclui um dispositivo por baixa explícita. Travar novas transferências de cerca durante uma transição real e ceder a prioridade a eventos confiáveis.
2. **Receber READY e ARM no perfil antigo.** Associar toda confirmação ao par sessão do Gateway/operação, ao ID e à sessão da coleira, ao perfil anterior e ao alvo. Gravar intenção e parâmetros antes de armar um horário relativo, tolerância de relógio e prazo de validade. Nenhum participante muda na fase READY. Confirmação atrasada, duplicada ou de outra sessão não avança o estado. Timeout encerra a tentativa ou inicia recuperação se já houve ARM parcial.
3. **Experimentar e verificar o alvo.** Abrir um intervalo de teste limitado. O Gateway só apresenta o perfil como operacional após receber evidência de comunicação em ambos os sentidos no alvo de cada participante. A licença de teste expira localmente. Implementar e testar uma política de renovação limitada e um caminho de retorno independente de uma última mensagem enviada no perfil novo. O modelo em host recebe provas abstratas dessa etapa.
4. **Recuperar e manter o sistema utilizável.** Diante de confirmação incompleta após armar, todos retornam ao perfil anterior por prazo local, inclusive se o rádio ficar em silêncio. Após reinício, o ponto comum especificado é P0. Um diário durável precisa registrar participantes e fase para reencontrar cada membro em P0, confirmar seu boot e só então liberar nova operação. Se alguém faltar, expor rollback sem verificação e continuar procurando. Esse desenho exige validar perda de mensagens de controle, relógios, reinício de qualquer lado e indisponibilidade prolongada. O modelo não comprova essas propriedades em RF.

## Preparação para a FPGA

O Raspberry poderá apresentar ao motor futuro registros por coleira com valores escalados, contagens, validade, produtor, sessão, janela, instante inicial no relógio de origem, idade e ID de perfil. A idade de recebimento/persistência tem de ser distinguida da idade no produtor. HDOP informa geometria do posicionamento. RSSI, SNR e PDR informam o enlace. PDR e RADIO têm o mesmo limite de relógio do Gateway e denominadores independentes. Associar registros exige verificar produtor, sessão, validade e tempo de cobertura; coincidência numérica de `windowId` sozinho é insuficiente. Qualquer recomendação futura precisa passar pela validação da Base e do coordenador global. Nenhuma implementação embarcada depende de FPGA.
