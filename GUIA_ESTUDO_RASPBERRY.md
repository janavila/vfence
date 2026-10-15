# Guia de estudo — Servidor VFence no Raspberry Pi

## 1. Objetivo deste guia

Este documento explica a parte do VFence destinada ao Raspberry Pi: como o servidor local está organizado, como os dados percorrem o sistema e como ocorrerá a comunicação com o ESP gateway e com o servidor web central.

O projeto deve ser estudado como três sistemas independentes que trocam mensagens:

1. **Servidor web central:** cadastro, acesso remoto e publicação de cercas.
2. **Raspberry Pi:** gateway de aplicação, banco local e painel operacional.
3. **Sistema embarcado:** ESP gateway, rádio LoRa e ESP das coleiras.

O Raspberry não substitui o ESP. O ESP cuida do rádio LoRa; o Raspberry cuida da aplicação, persistência, validação e sincronização.

---

## 2. Arquitetura prevista

### 2.1 Telemetria da coleira para o sistema

```text
GNSS da coleira
      ↓
ESP da coleira calcula o estado do geofencing
      ↓ LoRa
ESP gateway recebe o pacote
      ↓ USB/Serial
Raspberry interpreta o protocolo V1
      ↓
SQLite + API + WebSocket
      ↓
Dashboard local
```

### 2.2 Nova cerca do servidor para a coleira

```text
Fazendeiro desenha a cerca
      ↓
Servidor web publica uma nova versão
      ↓ HTTPS/API
Raspberry baixa e valida o vetor
      ↓ USB/Serial
ESP gateway recebe os vértices
      ↓ LoRa
ESP da coleira armazena e ativa a cerca
```

Esses dois fluxos são independentes. Uma falha de internet não deve impedir o monitoramento local nem invalidar a última cerca instalada.

---

## 3. O que está implementado no servidor local

A aplicação local está na pasta `app/` e utiliza FastAPI.

As principais funcionalidades existentes são:

- Inicialização do servidor HTTP local.
- Leitura do arquivo `.env`.
- Recepção de mensagens pela porta Serial.
- Modo mock para testes sem hardware.
- Interpretação do protocolo V1.
- Validação de mensagens de posicionamento.
- Banco de dados SQLite.
- Registro das coleiras, telemetria e eventos.
- API HTTP para consulta do estado do sistema.
- Atualizações em tempo real por WebSocket.
- Dashboard com mapa, coleiras e histórico de eventos.
- Reconexão da porta Serial.

A sincronização completa de cercas entre servidor, Raspberry e ESP ainda deve ser tratada como uma etapa de integração. A existência das telas e do editor não significa, sozinha, que o percurso completo já esteja finalizado.

---

## 4. Estrutura dos módulos do Raspberry

### 4.1 `app/main.py`

É o ponto de entrada da aplicação FastAPI.

Responsabilidades principais:

- Criar a instância da aplicação.
- Registrar páginas e endpoints.
- Preparar os arquivos estáticos e templates.
- Inicializar o banco de dados.
- Iniciar o leitor Serial ou o modo mock.
- Encerrar os recursos durante o desligamento.
- Entregar dados do gateway e das coleiras para a interface.

Ordem sugerida de estudo:

1. Localizar onde a aplicação FastAPI é criada.
2. Identificar o ciclo de vida de inicialização.
3. Listar todas as rotas HTTP.
4. Identificar a rota do WebSocket.
5. Observar como `main.py` chama banco, Serial e tempo real.

`main.py` deve coordenar os módulos, mas não concentrar toda a lógica de protocolo ou de persistência.

### 4.2 `app/config.py`

Centraliza a configuração carregada do ambiente.

Pode controlar informações como:

- Porta Serial.
- Velocidade da Serial.
- Ativação do modo mock.
- Caminho do SQLite.
- Host e porta HTTP.
- Intervalos de leitura e reconexão.

Ao estudar este arquivo, verifique:

- Quais variáveis possuem valor padrão.
- Quais precisam existir no `.env`.
- Quais valores são números, booleanos ou texto.
- Quais configurações mudam entre computador e Raspberry.

Senhas, tokens e segredos não devem ser escritos diretamente no código.

### 4.3 `app/serial_reader.py`

É a ligação entre o Raspberry e o ESP gateway.

Responsabilidades:

- Abrir a porta Serial.
- Ler linhas completas enviadas pelo ESP.
- Detectar desconexões.
- Tentar reconectar.
- Encaminhar mensagens válidas para o protocolo.
- Produzir mensagens simuladas no modo mock.

No Raspberry, a porta poderá aparecer como:

```text
/dev/ttyUSB0
```

ou:

```text
/dev/ttyACM0
```

O ponto mais importante é separar duas operações futuras:

- **Leitura:** telemetria enviada pelo ESP ao Raspberry.
- **Escrita:** comandos e vértices enviados pelo Raspberry ao ESP.

Hoje, a recepção de telemetria é a parte central. Para distribuir cercas, o módulo precisará de uma saída Serial controlada, com fila, confirmação e repetição em caso de erro.

### 4.4 `app/protocol.py`

Interpreta o protocolo V1.

Exemplo de mensagem:

```text
V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|1
```

Campos:

| Posição | Campo | Exemplo |
|---:|---|---|
| 1 | Versão | `V1` |
| 2 | Identificador | `COL01` |
| 3 | Tipo | `POS` |
| 4 | Latitude | `-31.306119` |
| 5 | Longitude | `-54.063935` |
| 6 | Estado | `SEGURO` |
| 7 | Satélites | `12` |
| 8 | HDOP | `0.90` |
| 9 | Sequência | `1` |

Estados internos:

- `SEGURO`
- `ATENCAO`
- `CRITICO`
- `FORA`
- `GNSS_INVALIDO`

Esses valores pertencem ao protocolo e não devem ser alterados apenas para mudar o texto exibido na tela.

Ao estudar o parser, acompanhe:

1. Separação da linha pelo delimitador `|`.
2. Conferência da quantidade de campos.
3. Validação da versão e do tipo.
4. Conversão de latitude, longitude, satélites, HDOP e sequência.
5. Validação dos intervalos.
6. Construção do objeto entregue ao restante do servidor.
7. Tratamento de mensagens inválidas.

### 4.5 `app/models.py`

Define as estruturas de dados utilizadas pela aplicação.

Os modelos mantêm um formato consistente entre:

- Mensagem interpretada.
- Lógica da aplicação.
- Resposta da API.
- Atualização do WebSocket.

Durante o estudo, observe quais campos são obrigatórios, quais aceitam ausência e quais são convertidos automaticamente.

### 4.6 `app/database.py`

Concentra o acesso ao SQLite.

O schema atual possui as tabelas:

- `collars`
- `telemetry`
- `events`

Função conceitual de cada tabela:

| Tabela | Finalidade |
|---|---|
| `collars` | Estado mais recente de cada coleira |
| `telemetry` | Histórico das mensagens recebidas |
| `events` | Mudanças relevantes de estado |

Fluxo esperado após uma posição válida:

1. Registrar a amostra em `telemetry`.
2. Atualizar o estado atual em `collars`.
3. Comparar o estado anterior com o novo.
4. Criar um registro em `events` se houver transição.

O schema existente deve permanecer compatível. Alterações futuras relacionadas às cercas devem ser planejadas sem modificar acidentalmente as três tabelas atuais.

### 4.7 `app/realtime.py`

Gerencia as conexões WebSocket do dashboard.

Quando uma nova telemetria é processada, o navegador pode ser atualizado imediatamente, sem recarregar a página.

Fluxo:

```text
Mensagem Serial
   → parser
   → banco
   → WebSocket
   → JavaScript do dashboard
```

Ao estudar este módulo, procure entender:

- Como clientes WebSocket são registrados.
- Como clientes desconectados são removidos.
- Qual objeto é transmitido.
- Como falhas em um navegador são isoladas dos demais.

### 4.8 `app/templates/`

Contém o HTML renderizado pelo servidor local.

O painel deve apresentar dados operacionais:

- Estado do gateway.
- Porta de comunicação.
- Coleiras ativas.
- Última mensagem.
- Posição no mapa.
- Qualidade GNSS.
- Estado do geofencing.
- Eventos recentes.
- Cerca recebida ou instalada.

### 4.9 `app/static/`

Contém CSS, JavaScript e recursos da interface.

O JavaScript do navegador normalmente é responsável por:

- Criar o mapa.
- Consultar a API inicial.
- Abrir o WebSocket.
- Atualizar marcadores.
- Atualizar estados e eventos.
- Desenhar o polígono da cerca.

O navegador não deve decidir o estado real da coleira. Ele apenas representa o estado recebido.

---

## 5. Modo mock

O modo mock simula mensagens sem utilizar ESP ou LoRa.

Ele permite estudar e testar:

- Protocolo V1.
- Banco de dados.
- Mudanças de estado.
- API.
- WebSocket.
- Dashboard.

No ambiente de produção, o modo mock deve ser desativado para que a fonte dos dados seja a porta Serial real.

É importante não confundir a simulação com o cálculo real de geofencing. A simulação apenas fornece mensagens parecidas com as que o hardware enviará.

---

## 6. Aplicação web central

A pasta `web/` representa uma aplicação separada do servidor local.

Ela poderá ser hospedada em um servidor acessível pela internet e usada pelo fazendeiro para:

- Autenticar-se.
- Administrar a propriedade.
- Desenhar a cerca no mapa.
- Publicar uma nova versão.
- Consultar rebanho, eventos e histórico.
- Verificar se a base está online.

Separação correta:

| Componente | Execução | Função principal |
|---|---|---|
| `web/` | Servidor de internet | Administração e publicação de cercas |
| `app/` | Raspberry Pi | Operação local e comunicação com o gateway |
| Firmware gateway | ESP conectado ao Raspberry | Serial e LoRa |
| Firmware da coleira | ESP no animal | GNSS, geofencing e telemetria |

As duas aplicações podem usar FastAPI e ainda assim permanecer independentes.

---

## 7. Como o Raspberry deverá receber a cerca

A alternativa inicial mais simples é o Raspberry consultar o servidor periodicamente por HTTPS.

Exemplo conceitual:

```text
GET /api/bases/BASE01/fence
Authorization: Bearer <token-da-base>
```

Resposta conceitual:

```json
{
  "fence_id": "CERCA01",
  "version": 3,
  "status": "published",
  "vertices": [
    [-313052296, -540643931],
    [-313054359, -540640980],
    [-313060134, -540646748],
    [-313058026, -540649538]
  ]
}
```

Os nomes definitivos dos endpoints e campos devem ser acordados entre as duas aplicações. O exemplo acima não substitui essa especificação.

Processamento seguro no Raspberry:

1. Autenticar a base.
2. Consultar a versão disponível.
3. Comparar com a versão local.
4. Baixar somente uma versão mais nova.
5. Validar identificador, quantidade e coordenadas.
6. Salvar como atualização pendente.
7. Enviar ao ESP gateway.
8. Esperar a confirmação da coleira.
9. Marcar a versão como instalada.
10. Informar o resultado ao servidor.

O Raspberry não deve apagar a cerca anterior antes de confirmar que a nova foi recebida corretamente.

---

## 8. Coordenadas no formato E7

Para o transporte embarcado, latitude e longitude podem ser representadas como inteiros E7.

Conversão:

```text
coordenada_e7 = coordenada_em_graus × 10.000.000
```

Exemplo:

```text
-31.3052296 → -313052296
-54.0643931 → -540643931
```

Conversão inversa:

```text
coordenada_em_graus = coordenada_e7 / 10.000.000
```

Vantagens:

- Representação determinística.
- Menos problemas de ponto flutuante.
- Facilidade de validação.
- Compatibilidade com armazenamento embarcado.

O sistema precisa definir claramente onde a conversão acontece para evitar multiplicar uma coordenada que já está em E7.

---

## 9. Transporte da cerca pela Serial e pelo LoRa

O JSON recebido do servidor não deve ser enviado diretamente em um único pacote LoRa.

Uma cerca pode precisar ser dividida em:

1. Mensagem de início.
2. Uma ou mais mensagens de vértices.
3. Mensagem de finalização.
4. Confirmação de integridade.

Cada fragmento deverá possuir dados suficientes para reconstrução:

- Identificador da cerca.
- Versão.
- Índice do vértice.
- Total de vértices.
- Latitude E7.
- Longitude E7.
- Sequência.
- Verificação de integridade.

Também será necessário decidir:

- Tamanho máximo de cada pacote.
- Tempo de espera pela confirmação.
- Quantidade de novas tentativas.
- Comportamento após reinício.
- Forma de detectar vértices ausentes.
- Momento em que a nova cerca passa a valer.

Essa extensão deve ser projetada sem quebrar as mensagens de telemetria do protocolo V1.

---

## 10. Operação sem internet

O sistema foi concebido para manter a operação local.

Sem internet:

- A coleira usa a última cerca válida.
- O gateway continua recebendo LoRa.
- O Raspberry continua recebendo Serial.
- O SQLite continua registrando telemetria.
- O dashboard local continua acessível na rede da propriedade.
- A sincronização externa fica pendente.

Quando a conexão retornar, o Raspberry poderá consultar novas cercas e enviar os dados acumulados.

O servidor web não deve participar de cada decisão de dentro ou fora da cerca. Essa decisão precisa continuar disponível localmente.

---

## 11. Segurança da comunicação

O vínculo entre servidor central e Raspberry deve utilizar:

- HTTPS.
- Identificador único da base.
- Token individual.
- Segredos armazenados no `.env`.
- Validação rigorosa da resposta.
- Controle de versão.
- Registro de erros sem exposição de credenciais.

O Raspberry normalmente inicia a conexão com o servidor. Assim, não é necessário expor diretamente o Raspberry à internet nem abrir uma porta pública na propriedade.

Tokens do servidor não devem ser enviados às coleiras pelo LoRa.

---

## 12. Testes existentes

Os primeiros arquivos a estudar na pasta `tests/` são:

- `tests/test_protocol.py`
- `tests/test_api.py`

### `test_protocol.py`

Deve demonstrar:

- Formato aceito pelo parser.
- Conversões realizadas.
- Mensagens rejeitadas.
- Limites dos campos.

### `test_api.py`

Deve demonstrar:

- Rotas públicas da aplicação.
- Formato dos JSONs.
- Códigos HTTP esperados.
- Estado inicial do servidor.

Ler os testes antes de alterar o código é uma maneira rápida de descobrir o contrato que não pode ser quebrado.

---

## 13. Firmware relacionado

### `Testelora/receiver/receiver.ino`

Representa o lado receptor/gateway dos testes LoRa.

Ao estudá-lo, localize:

- Inicialização do rádio.
- Frequência configurada.
- Pinos utilizados.
- Recepção do pacote.
- Escrita na Serial.
- RSSI e SNR.

Para integrar ao Raspberry, a saída Serial precisa ter um formato estável e sem textos de depuração misturados às mensagens de dados.

### `geofence/vfence.ino`

Contém a lógica embarcada de geofencing em estudo.

Pontos importantes:

- Representação do polígono.
- Ordem dos vértices.
- Conversão das coordenadas.
- Algoritmo de ponto dentro do polígono.
- Tratamento da margem de atenção e margem crítica.
- Comportamento quando o GNSS é inválido.

### `testeconexao/testeconexao.ino`

É um código experimental. Deve ser estudado como teste de hardware ou comunicação, e não automaticamente como firmware final de produção.

---

## 14. Como executar localmente para estudo

Na raiz do projeto, com o ambiente Python preparado:

```powershell
& "C:\Users\leobo\AppData\Local\Programs\Python\Python312\python.exe" -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Depois, abrir:

```text
http://127.0.0.1:8000
```

Durante o estudo, acompanhe simultaneamente:

1. As linhas recebidas no terminal.
2. O conteúdo do dashboard.
3. Os registros do banco.
4. As atualizações de estado.

Para encerrar:

```text
Ctrl+C
```

---

## 15. Ordem recomendada de estudo

### Etapa 1 — Entender uma mensagem

Começar por `app/protocol.py` e `tests/test_protocol.py`.

Objetivo: compreender completamente uma linha V1.

### Etapa 2 — Acompanhar a entrada

Estudar `app/serial_reader.py`.

Objetivo: descobrir como uma linha sai do ESP e chega ao parser.

### Etapa 3 — Acompanhar o armazenamento

Estudar `app/database.py` e `app/models.py`.

Objetivo: verificar como telemetria, estado atual e eventos são registrados.

### Etapa 4 — Acompanhar a exibição

Estudar `app/realtime.py`, templates e JavaScript.

Objetivo: entender como um dado novo aparece na tela.

### Etapa 5 — Entender a coordenação

Estudar `app/main.py` e `tests/test_api.py`.

Objetivo: entender como todos os módulos são inicializados e expostos.

### Etapa 6 — Comparar com o hardware

Estudar `receiver.ino`, `vfence.ino` e os testes de conexão.

Objetivo: alinhar o formato transmitido com o formato esperado pelo servidor.

### Etapa 7 — Estudar a aplicação central

Estudar `web/README.md` e o backend da pasta `web/`.

Objetivo: definir o contrato entre o servidor central e a base.

---

## 16. Exercício prático de rastreamento

Utilizar a mensagem:

```text
V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|1
```

Responder, consultando o código:

1. Qual função recebe a linha?
2. Qual função divide os campos?
3. Qual modelo representa o resultado?
4. Onde a telemetria é gravada?
5. Onde o estado de `COL01` é atualizado?
6. Quando um evento é criado?
7. Qual JSON é enviado ao navegador?
8. Qual função atualiza o marcador no mapa?

Quando essas oito respostas estiverem claras, o fluxo atual do Raspberry estará compreendido.

---

## 17. Funcionalidades futuras que não devem ser confundidas com as atuais

Ainda precisam de implementação ou validação ponta a ponta:

- Cliente HTTPS do Raspberry para o servidor central.
- Consulta e download automático de cercas.
- Persistência local das versões de cerca.
- Escrita da cerca na porta Serial.
- Protocolo de comandos Raspberry–ESP.
- Fragmentação dos vértices para LoRa.
- Confirmação e retransmissão.
- Ativação atômica da cerca na coleira.
- Retorno de implantação ao servidor.
- Sincronização externa da telemetria acumulada.
- Teste real de falha de energia e de internet.

Esses itens devem ser construídos em módulos separados para preservar a estabilidade do servidor já existente.

---

## 18. Resumo técnico

O Raspberry é a central local do VFence.

Ele recebe telemetria do ESP gateway pela Serial, interpreta o protocolo V1, salva as informações no SQLite e atualiza o dashboard por WebSocket.

Na integração futura, ele também consultará o servidor web central, receberá a versão publicada da cerca, validará o vetor e o encaminhará ao ESP gateway. O gateway transmitirá a cerca por LoRa às coleiras.

O projeto deve conservar autonomia local: a última cerca válida, a comunicação LoRa, o banco e o dashboard precisam continuar funcionando durante uma interrupção da internet.
