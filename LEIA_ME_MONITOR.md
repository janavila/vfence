# VFENCE Protocol Monitor — instalação e uso

## Instalação

Esta atualização parte do `vfence.rar` enviado anteriormente, com o patch `VFENCE_Integracao_Journal_Windows.zip` já aplicado e validado em hardware pelo usuário. Não é necessário executar novamente o patch anterior.

1. Encerre a Base Windows (`Ctrl+C`) e aguarde o término normal. Faça backup da pasta `app/` e do diretório `logs/`, sem apagar o `vfence.db`.
2. Extraia este ZIP na **raiz do projeto VFENCE** (a pasta onde está `vfence_windows.py`). Aceite a substituição de `app/main.py`, `app/serial_reader.py` e `app/packet_journal.py`. Os outros arquivos são novos. Se a equipe alterou esses três arquivos depois da versão enviada, faça merge das alterações em vez de sobrescrever trabalho mais recente.
3. Execute exatamente o comando já utilizado:

   ```powershell
   python vfence_windows.py
   ```

   Não é necessário iniciar outro Python, abrir a COM novamente, instalar SQLite ou instalar bibliotecas adicionais, desde que a Base original já esteja funcionando.
4. Abra no navegador do próprio computador da Base: `http://127.0.0.1:8000/monitor`. Caso a porta configurada seja diferente, use a porta da Base. O monitor é somente leitura e, por segurança, aceita apenas clientes locais. Não precisa de internet. O terminal e os comandos `liberar` e `reconectar` continuam como antes.

## Onde estão os dados

O banco `logs/vfence_packets.db` é criado automaticamente, em modo WAL. Os arquivos auxiliares `vfence_packets.db-wal` e `vfence_packets.db-shm` podem aparecer enquanto o banco está aberto. Não os apague manualmente. Cada execução recebe um identificador de sessão, mas não cria outro banco. O banco operacional `vfence.db` não é alterado pelo monitor.

Os antigos `vfence_packets_*.jsonl` e `vfence_packets_*.csv` devem permanecer em `logs/`. A importação automática ocorre em uma thread separada. É preferido o JSONL quando há um CSV com o mesmo nome-base (não são importados os dois). Se apenas o CSV estiver disponível, ele é importado. A importação é idempotente por nome-base/linha e ignora cópias idênticas de arquivos já importados. Não edite ou reordene arquivos antigos durante a importação. Linhas malformadas são contabilizadas e os originais preservados; um CSV companheiro não é usado para reconstruir linhas faltantes no JSONL. Logs distintos que contenham registros parcialmente sobrepostos podem precisar de curadoria. Não apague os logs antigos antes de conferir as sessões e importações na aba Histórico.

## Uso

- **Visão geral:** totais observados de RX, tentativas TX, retransmissões e médias de RSSI/SNR.
- **Pacotes e inspector:** categoria, subtipo, direção, coleira, perfil, sessão, período, sequence, RETRY e validade informada pelo gateway. Clique numa mensagem e nos blocos coloridos. Cabeçalho e payload possuem separação por campo, offsets dos bytes, legenda e valores decodificados. O hexadecimal e o JSON originais são preservados. Exporte todos os pacotes que atendem aos filtros em CSV.
- **Métricas:** RSSI, SNR, satélites, HDOP, PDR estimado por janela, bateria e distância da borda, em gráficos com eixo temporal sincronizado e série por coleira. As médias de janela são exibidas separadamente das amostras individuais ao ativar a opção correspondente. Pontos são atualizados por consulta a cada dois segundos. A janela de tempo e a quantidade de pontos podem ser limitadas. Gráficos antigos dependem dos campos existentes nos registros importados; PDR e médias de janela passam a ser coletados a partir desta atualização, se o firmware exportá-los. Satélites/HDOP só são plotados para telemetria com GPS válido e valores disponíveis.
- **Transições de perfil:** sequência de mensagens por sessão do gateway, operação e coleira, com direção, fase, perfil e duração observada. O filtro de perfil é ignorado nessa aba para não ocultar fases de uma operação que cruza perfis. Os filtros de período/sessão podem limitar o contexto. VERIFIED e RESTORED observados não provam o sucesso de todas as coleiras. Ausência de um quadro não comprova falha de rádio.
- **Histórico / comparação:** grupos por sessão, perfil e coleira, falhas/descartes de captura e importações. A comparação é descritiva dos pacotes registrados, não é PDR. Se houver muitos grupos ou pontos, a interface indica o limite e permite refinar os filtros.

O monitor recebe apenas cópias de mensagens `packet_log` e de resumos de métricas/PDR exportados pelo firmware. A fila de captura é limitada a 2.048 registros; a gravação usa uma thread separada. Caso a fila sature, o SQLite falhe ou um registro não seja compatível, o sistema operacional da Base continua e o journal pode emitir avisos. Encerrar normalmente permite drenar a fila. A captura não promete registrar pacotes perdidos no ar ou descartados antes de chegar à Base. O tempo mostrado é o timestamp UTC da recepção no PC, convertido para a hora local pelo navegador; não é o instante exato de emissão RF. O decodificador é observacional, não substitui a validação do firmware, e mantém bytes desconhecidos sem interpretação especulativa.

## Validação e reversão

Foram executados quatro testes novos, com dados artificiais, e 78 testes existentes (mais 47 subtestes) aprovados. Um teste existente de API falhou também sem a modificação do `main.py` (`metrics_gateway_id` ausente no mock); sete testes não puderam ser executados corretamente porque `pyserial` não está instalado no ambiente de validação. A interface foi exercitada em Chromium com respostas da API de teste injetadas, porque o navegador do ambiente bloqueou navegação local direta. As rotas foram testadas com TestClient. A integração real no Windows, com hardware Gateway/FPGA, ainda precisa ser verificada.

Após instalar: verifique primeiro o terminal e a recepção; abra o monitor, confirme que os pacotes aparecem, execute uma troca de perfil pela interface já usada pelo projeto e compare as fases registradas; feche e reabra a Base e confirme que o histórico permanece. Se necessário, restaure os três arquivos do backup e remova os novos módulos/página; preserve `logs/` e `vfence.db`. Não sobrescreva um banco que contenha dados coletados.

Teste automatizado opcional, com as dependências de desenvolvimento já existentes:

```powershell
python -m pytest tests/test_protocol_monitor.py -q
```
