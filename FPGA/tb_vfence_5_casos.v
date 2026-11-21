`timescale 1ns/1ps
// VFence - Testbench didatico com 5 avaliacoes de 1 coleira (P1).
// Compatibilidade: Verilog-2001 / ModelSim-Altera / Quartus II 13.1.
module tb_vfence_5_casos;
    reg clk;
    reg reset_n;
    reg sample_valid;
    reg window_close;
    reg result_ack;
    reg [1:0] current_profile;
    reg [15:0] pdr_x100;
    reg signed [15:0] snr_x100;
    reg [7:0] rssi_mag;
    reg [7:0] sample_count;
    reg [2:0] metrics_valid;

    wire [1:0] recommended_profile;
    wire [2:0] decision_reason;
    wire result_ready;
    wire critical_alert;
    wire [8:0] q_debug;
    wire [1:0] score_pdr_debug;
    wire [1:0] score_snr_debug;
    wire [1:0] score_rssi_debug;
    wire [1:0] vote_profile_debug;
    wire vote_valid_debug;
    wire [2:0] votos_p0;
    wire [2:0] votos_p1;
    wire [2:0] votos_p2;
    wire [2:0] votos_p3;
    wire [2:0] eval_count;

    integer erros;
    integer casos_executados;

    vfence_top dut (
        .clk(clk),
        .reset_n(reset_n),
        .sample_valid(sample_valid),
        .window_close(window_close),
        .result_ack(result_ack),
        .current_profile(current_profile),
        .pdr_x100(pdr_x100),
        .snr_x100(snr_x100),
        .rssi_mag(rssi_mag),
        .sample_count(sample_count),
        .metrics_valid(metrics_valid),
        .recommended_profile(recommended_profile),
        .decision_reason(decision_reason),
        .result_ready(result_ready),
        .critical_alert(critical_alert),
        .q_debug(q_debug),
        .score_pdr_debug(score_pdr_debug),
        .score_snr_debug(score_snr_debug),
        .score_rssi_debug(score_rssi_debug),
        .vote_profile_debug(vote_profile_debug),
        .vote_valid_debug(vote_valid_debug),
        .votos_p0(votos_p0),
        .votos_p1(votos_p1),
        .votos_p2(votos_p2),
        .votos_p3(votos_p3),
        .eval_count(eval_count)
    );

    initial clk = 1'b0;
    always #5 clk = ~clk; // periodo = 10 ns

    // epdr, esnr, erssi, eq, evoto sao os resultados esperados pelo
    // modelo aritmetico independente, usados para conferir o circuito.
    task executar_caso;
        input integer numero;
        input [15:0] pdr_in;
        input integer snr_in;
        input [7:0] rssi_in;
        input [1:0] epdr;
        input [1:0] esnr;
        input [1:0] erssi;
        input [8:0] eq;
        input [1:0] evoto;
        begin
            // Altera entradas fora da borda ativa do clock.
            @(negedge clk);
            pdr_x100     = pdr_in;
            snr_x100     = snr_in;
            rssi_mag     = rssi_in;
            sample_valid = 1'b1;               // 1 pulso = 1 avaliacao
            window_close = (numero == 5);     // apenas o ultimo fecha a janela

            @(posedge clk);
            #1; // espera as atualizacoes dos registradores
            $display("----------------------------------------------------------------");
            $display("TESTE %0d | P1 | PDR=%0d/100 %% | SNR=%0d/100 dB | RSSI=-%0d dBm",
                     numero, pdr_in, snr_in, rssi_in);
            $display("Scores: P=%0d S=%0d R=%0d | Q=%0d/300 | Voto=P%0d | Valido=%0d",
                     score_pdr_debug, score_snr_debug, score_rssi_debug,
                     q_debug, vote_profile_debug, vote_valid_debug);
            $display("Votos acumulados: P0=%0d P1=%0d P2=%0d P3=%0d | avaliacoes=%0d",
                     votos_p0, votos_p1, votos_p2, votos_p3, eval_count);

            if ((score_pdr_debug !== epdr) ||
                (score_snr_debug !== esnr) ||
                (score_rssi_debug !== erssi) ||
                (q_debug !== eq) ||
                (vote_profile_debug !== evoto) ||
                (vote_valid_debug !== 1'b1) ||
                (eval_count !== numero)) begin
                $display("FALHA NO TESTE %0d: resultado diferente do esperado!", numero);
                $display("Esperado: P=%0d S=%0d R=%0d Q=%0d voto=P%0d",
                         epdr, esnr, erssi, eq, evoto);
                erros = erros + 1;
            end else begin
                $display("TESTE %0d: PASSOU (conferencia automatica)", numero);
            end
            casos_executados = casos_executados + 1;
            @(negedge clk);
            sample_valid = 1'b0;
            window_close = 1'b0;
        end
    endtask

    initial begin
        reset_n          = 1'b0;
        sample_valid     = 1'b0;
        window_close     = 1'b0;
        result_ack       = 1'b0;
        current_profile  = 2'd1;     // P1: SF10, SNR lim=-15, RSSI sens=-132
        pdr_x100         = 16'd0;
        snr_x100         = 16'sd0;
        rssi_mag         = 8'd0;
        sample_count     = 8'd5;     // cinco telemetrias recebidas por minuto
        metrics_valid    = 3'b111;   // PDR, SNR e RSSI validos
        erros            = 0;
        casos_executados = 0;

        repeat (2) @(negedge clk);
        reset_n = 1'b1;
        $display("INICIO - VFence: 5 avaliacoes (perfil atual P1)");
        $display("Lembrete: Q = 50*P + 35*S + 15*R");

        //                 PDRx100   SNRx100 RSSI  P  S  R     Q   voto
        executar_caso(1, 16'd7800,  -1100, 8'd115, 2'd1,2'd2,2'd3, 9'd165,2'd2);
        executar_caso(2, 16'd9800,   -500, 8'd105, 2'd3,2'd3,2'd3, 9'd300,2'd0);
        executar_caso(3, 16'd8500,  -1400, 8'd125, 2'd2,2'd1,2'd2, 9'd165,2'd2);
        executar_caso(4, 16'd9600,  -1200, 8'd128, 2'd3,2'd2,2'd1, 9'd235,2'd1);
        executar_caso(5, 16'd4900,  -1800, 8'd136, 2'd0,2'd0,2'd0, 9'd0,  2'd2);

        // O fechamento aconteceu no caso 5. No proximo clock a UC captura
        // o resultado de maioria calculado pela UO.
        @(posedge clk);
        #1;
        $display("================================================================");
        $display("DECISAO FINAL: P0=%0d P1=%0d P2=%0d P3=%0d",
                 votos_p0, votos_p1, votos_p2, votos_p3);
        $display("result_ready=%0d | recommended_profile=P%0d | decision_reason=%0d | critical_alert=%0d",
                 result_ready, recommended_profile, decision_reason, critical_alert);
        if ((votos_p0 !== 3'd1) || (votos_p1 !== 3'd1) ||
            (votos_p2 !== 3'd3) || (votos_p3 !== 3'd0) ||
            (result_ready !== 1'b1) || (recommended_profile !== 2'd2) ||
            (decision_reason !== 3'b001) || (critical_alert !== 1'b0)) begin
            $display("FALHA: resultado da janela nao confere.");
            erros = erros + 1;
        end else begin
            $display("JANELA: PASSOU (maioria de 3 votos para P2)");
        end

        // Teste adicional: confirmar leitura e limpar contadores.
        @(negedge clk);
        result_ack = 1'b1;
        @(posedge clk);
        #1;
        if (result_ready !== 1'b0 || eval_count !== 3'd0 ||
            votos_p0 !== 3'd0 || votos_p1 !== 3'd0 ||
            votos_p2 !== 3'd0 || votos_p3 !== 3'd0) begin
            $display("FALHA: contadores nao foram limpos com result_ack.");
            erros = erros + 1;
        end else begin
            $display("LIMPEZA: PASSOU (contadores reiniciados)");
        end
        @(negedge clk);
        result_ack = 1'b0;

        $display("================================================================");
        if (erros == 0)
            $display("RESULTADO: %0d/5 casos conferidos; janela e limpeza OK.", casos_executados);
        else
            $display("RESULTADO: %0d erro(s) encontrados.", erros);
        $finish;
    end
endmodule
