`timescale 1ns/1ps
// Testbench Verilog-2001: multicoleiras, quatro metricas, desempate e protecao.
module tb_vfence_multicoleiras;
    reg clk, reset_n, sample_valid, window_close, result_ack;
    reg [1:0] current_profile;
    reg [7:0] collar_id;
    reg [2:0] minute_index;
    reg [15:0] pdr_x100;
    reg signed [15:0] snr_x100;
    reg [7:0] rssi_mag;
    reg [15:0] hdop_x100;
    reg [7:0] sample_count;
    reg [3:0] metrics_valid;
    wire [1:0] recommended_profile, score_pdr_debug, score_snr_debug;
    wire [1:0] score_rssi_debug, score_hdop_debug, vote_profile_debug;
    wire [2:0] decision_reason;
    wire result_ready, critical_alert, vote_valid_debug, duplicate_debug;
    wire [8:0] q_debug;
    wire [10:0] votos_p0, votos_p1, votos_p2, votos_p3, eval_count;
    integer erros;
    integer minuto;

    vfence_top dut (
        .clk(clk), .reset_n(reset_n), .sample_valid(sample_valid),
        .window_close(window_close), .result_ack(result_ack),
        .current_profile(current_profile), .collar_id(collar_id),
        .minute_index(minute_index), .pdr_x100(pdr_x100),
        .snr_x100(snr_x100), .rssi_mag(rssi_mag), .hdop_x100(hdop_x100),
        .sample_count(sample_count), .metrics_valid(metrics_valid),
        .recommended_profile(recommended_profile), .decision_reason(decision_reason),
        .result_ready(result_ready), .critical_alert(critical_alert),
        .q_debug(q_debug), .score_pdr_debug(score_pdr_debug),
        .score_snr_debug(score_snr_debug), .score_rssi_debug(score_rssi_debug),
        .score_hdop_debug(score_hdop_debug),
        .vote_profile_debug(vote_profile_debug), .vote_valid_debug(vote_valid_debug),
        .duplicate_debug(duplicate_debug), .votos_p0(votos_p0),
        .votos_p1(votos_p1), .votos_p2(votos_p2),
        .votos_p3(votos_p3), .eval_count(eval_count)
    );

    initial clk = 1'b0;
    always #5 clk = ~clk;

    task resetar;
        begin
            @(negedge clk);
            reset_n = 1'b0;
            sample_valid = 1'b0;
            window_close = 1'b0;
            result_ack = 1'b0;
            @(negedge clk);
            reset_n = 1'b1;
        end
    endtask

    task avaliar;
        input [7:0] id;
        input [2:0] min_idx;
        input [15:0] pdr;
        input integer snr;
        input [7:0] rssi;
        input [15:0] hdop;
        input [3:0] valids;
        begin
            @(negedge clk);
            collar_id = id;
            minute_index = min_idx;
            pdr_x100 = pdr;
            snr_x100 = snr;
            rssi_mag = rssi;
            hdop_x100 = hdop;
            metrics_valid = valids;
            sample_valid = 1'b1;
            @(posedge clk);
            #1;
            @(negedge clk);
            sample_valid = 1'b0;
        end
    endtask

    task fechar;
        begin
            @(negedge clk);
            window_close = 1'b1;
            @(posedge clk);
            #1;
            @(negedge clk);
            window_close = 1'b0;
            @(posedge clk);
            #1;
        end
    endtask

    task confirmar;
        begin
            @(negedge clk);
            result_ack = 1'b1;
            @(posedge clk);
            #1;
            if (result_ready !== 1'b0 || eval_count !== 11'd0 ||
                votos_p0 !== 11'd0 || votos_p1 !== 11'd0 ||
                votos_p2 !== 11'd0 || votos_p3 !== 11'd0) begin
                $display("FALHA: ACK nao limpou janela");
                erros = erros + 1;
            end
            @(negedge clk);
            result_ack = 1'b0;
        end
    endtask

    task conferir_final;
        input [10:0] p0;
        input [10:0] p1;
        input [10:0] p2;
        input [10:0] p3;
        input [10:0] n;
        input [1:0] profile;
        input [2:0] reason;
        input crit;
        begin
            $display("VOTOS: P0=%0d P1=%0d P2=%0d P3=%0d | entradas=%0d",
                     votos_p0,votos_p1,votos_p2,votos_p3,eval_count);
            $display("RESULTADO: ready=%0d, P%0d, reason=%0d, critical=%0d",
                     result_ready,recommended_profile,decision_reason,critical_alert);
            if ((votos_p0 !== p0) || (votos_p1 !== p1) ||
                (votos_p2 !== p2) || (votos_p3 !== p3) ||
                (eval_count !== n) || (recommended_profile !== profile) ||
                (decision_reason !== reason) || (critical_alert !== crit) ||
                (result_ready !== 1'b1)) begin
                $display("FALHA: resultado divergente");
                erros = erros + 1;
            end else
                $display("PASSOU: resultado conferido");
        end
    endtask

    initial begin
        reset_n = 1'b0;
        sample_valid = 1'b0;
        window_close = 1'b0;
        result_ack = 1'b0;
        current_profile = 2'd1;
        collar_id = 8'd1;
        minute_index = 3'd0;
        pdr_x100 = 16'd0;
        snr_x100 = 16'sd0;
        rssi_mag = 8'd0;
        hdop_x100 = 16'd0;
        sample_count = 8'd5;
        metrics_valid = 4'b1111;
        erros = 0;
        repeat (2) @(negedge clk);
        reset_n = 1'b1;

        $display("=== TESTE 1: 3 COLEIRAS x 5 MINUTOS; 10 VOTOS P2, 5 P0 ===");
        for (minuto=0; minuto<5; minuto=minuto+1) begin
            // PDR 78% -> P2 mesmo com Q=180 (peso HDOP incluido).
            avaliar(8'd1,minuto,16'd7800,-1100,8'd115,16'd140,4'b1111);
            if (minuto==0 && (q_debug !== 9'd180 || score_hdop_debug !== 2'd3)) begin
                $display("FALHA: score HDOP ou Q nao esperado");
                erros = erros + 1;
            end
            avaliar(8'd2,minuto,16'd7800,-1100,8'd115,16'd140,4'b1111);
            // PDR 98%, SNR/RSSI acima do limiar de P0 -> P0.
            avaliar(8'd3,minuto,16'd9800,-500,8'd105,16'd100,4'b1111);
            if (minuto==0) begin
                avaliar(8'd1,minuto,16'd9800,-500,8'd105,16'd100,4'b1111);
                if (eval_count !== 11'd3 || votos_p2 !== 11'd2 ||
                    votos_p0 !== 11'd1 || duplicate_debug !== 1'b1) begin
                    $display("FALHA: voto duplicado nao foi bloqueado");
                    erros = erros + 1;
                end
            end
        end
        fechar();
        conferir_final(11'd5,11'd0,11'd10,11'd0,11'd15,2'd2,3'b001,1'b0);
        confirmar();

        $display("=== TESTE 2: EMPATE P0/P2 (5/5) MANTEM P1 ===");
        for (minuto=0; minuto<5; minuto=minuto+1) begin
            avaliar(8'd1,minuto,16'd7800,-1100,8'd115,16'd140,4'b1111);
            avaliar(8'd2,minuto,16'd9800,-500,8'd105,16'd100,4'b1111);
        end
        fechar();
        conferir_final(11'd5,11'd0,11'd5,11'd0,11'd10,2'd1,3'b101,1'b0);
        confirmar();

        $display("=== TESTE 3: CRITICO SO PARA MESMA COLEIRA ===");
        avaliar(8'd1,3'd0,16'd4900,-1800,8'd136,16'd150,4'b1111);
        avaliar(8'd2,3'd1,16'd4900,-1800,8'd136,16'd150,4'b1111);
        if (result_ready !== 1'b0) begin
            $display("FALHA: duas coleiras distintas ativaram critico");
            erros = erros + 1;
        end
        avaliar(8'd1,3'd1,16'd4900,-1800,8'd136,16'd150,4'b1111);
        @(posedge clk); #1;
        conferir_final(11'd0,11'd0,11'd3,11'd0,11'd3,2'd1,3'b011,1'b1);
        confirmar();

        $display("=== TESTE 4: HDOP INVALIDO NAO GERA VOTO ===");
        for (minuto=0; minuto<5; minuto=minuto+1)
            avaliar(8'd1,minuto,16'd9800,-500,8'd105,16'd100,4'b0111);
        fechar();
        conferir_final(11'd0,11'd0,11'd0,11'd0,11'd0,2'd1,3'b100,1'b0);
        confirmar();

        $display("=== TESTE 5: ID INVALIDO E MINUTO INVALIDO ===");
        avaliar(8'd0,3'd0,16'd9800,-500,8'd105,16'd100,4'b1111);
        avaliar(8'd255,3'd0,16'd9800,-500,8'd105,16'd100,4'b1111);
        avaliar(8'd1,3'd5,16'd9800,-500,8'd105,16'd100,4'b1111);
        fechar();
        conferir_final(11'd0,11'd0,11'd0,11'd0,11'd0,2'd1,3'b100,1'b0);
        confirmar();

        $display("=== SUFICIENCIA, CORRECAO E LIMITES ===");
        fechar();
        conferir_final(0,0,0,0,0,1,4,0);
        confirmar();
        avaliar(1,0,7800,-1100,115,140,4'b1111);
        fechar();
        conferir_final(0,0,1,0,1,1,4,0);
        confirmar();
        avaliar(1,0,7800,-1100,115,140,4'b1111);
        avaliar(2,0,7800,-1100,115,140,4'b1111);
        fechar();
        conferir_final(0,0,2,0,2,1,4,0);
        confirmar();
        avaliar(1,0,7800,-1100,115,140,4'b1111);
        avaliar(2,0,7800,-1100,115,140,4'b1111);
        avaliar(3,0,7800,-1100,115,140,4'b1111);
        fechar();
        conferir_final(0,0,3,0,3,1,4,0);
        confirmar();
        avaliar(1,2,7800,-1100,115,140,4'b1111);
        avaliar(1,0,7800,-1100,115,140,4'b0111);
        avaliar(1,0,7800,-1100,115,16'hffff,4'b1111);
        avaliar(1,0,10001,-1100,115,140,4'b1111);
        avaliar(1,0,7800,-1100,115,140,4'b1111);
        avaliar(1,1,7800,-1100,115,140,4'b1111);
        fechar();
        conferir_final(0,0,3,0,3,2,1,0);
        confirmar();
        avaliar(1,0,7800,-1100,115,140,4'b1111);
        avaliar(2,0,7800,-1100,115,140,4'b1111);
        avaliar(1,1,7800,-1100,115,140,4'b1111);
        fechar();
        conferir_final(0,0,3,0,3,2,1,0);
        confirmar();

        $display("=== PDR CRITICO INDEPENDENTE, DEDUPLICADO E FORA DE ORDEM ===");
        avaliar(1,1,4900,-32768,255,16'hffff,4'b0100);
        avaliar(1,1,9800,32767,0,100,4'b0100);
        avaliar(1,0,4900,-32768,255,16'hffff,4'b0100);
        @(posedge clk); #1;
        conferir_final(0,0,0,0,0,1,3,1);
        confirmar();

        $display("===============================================================");
        if (erros==0) $display("PASSOU: todos os cenarios do testbench multicoleiras");
        else $display("FALHOU: %0d erro(s)",erros);
        $finish;
    end
endmodule
