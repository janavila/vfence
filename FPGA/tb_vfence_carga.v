`timescale 1ns/1ps
// Testbench de carga VFence: 10, 20, 50 e 254 coleiras.
// Arquivo de SIMULACAO. Nao entra na sintese da FPGA.
// Compativel com Verilog-2001 e ModelSim-Altera 10.1d.
module tb_vfence_carga;
    reg clk, reset_n, sample_valid, window_close, result_ack;
    reg [1:0] current_profile;
    reg [7:0] collar_id;
    reg [2:0] minute_index;
    reg [15:0] pdr_x100, hdop_x100;
    reg signed [15:0] snr_x100;
    reg [7:0] rssi_mag, sample_count;
    reg [3:0] metrics_valid;

    wire [1:0] recommended_profile, vote_profile_debug;
    wire [1:0] score_pdr_debug, score_snr_debug, score_rssi_debug, score_hdop_debug;
    wire [2:0] decision_reason;
    wire [8:0] q_debug;
    wire result_ready, critical_alert, vote_valid_debug, duplicate_debug;
    wire [10:0] votos_p0, votos_p1, votos_p2, votos_p3, eval_count;
    integer erros, casos_ok;
    integer idx, minuto, lim_p2;

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
        .score_hdop_debug(score_hdop_debug), .vote_profile_debug(vote_profile_debug),
        .vote_valid_debug(vote_valid_debug), .duplicate_debug(duplicate_debug),
        .votos_p0(votos_p0), .votos_p1(votos_p1), .votos_p2(votos_p2),
        .votos_p3(votos_p3), .eval_count(eval_count)
    );

    initial clk = 1'b0;
    always #5 clk = ~clk;

    // tipo=0: metricas otimas, voto P0 a partir do perfil atual P1.
    // tipo=2: PDR 78%, voto P2 a partir do perfil atual P1.
    task enviar;
        input [7:0] id;
        input [2:0] min_idx;
        input integer tipo;
        begin
            @(negedge clk);
            collar_id = id;
            minute_index = min_idx;
            sample_count = 8'd5;
            metrics_valid = 4'b1111;
            if (tipo == 2) begin
                pdr_x100 = 16'd7800;
                snr_x100 = -16'sd1100;
                rssi_mag = 8'd115;
                hdop_x100 = 16'd140;
            end else begin
                pdr_x100 = 16'd9800;
                snr_x100 = -16'sd500;
                rssi_mag = 8'd105;
                hdop_x100 = 16'd100;
            end
            sample_valid = 1'b1;
            @(negedge clk);
            sample_valid = 1'b0;
        end
    endtask

    task fechar_janela;
        begin
            @(negedge clk);
            window_close = 1'b1;
            @(negedge clk);
            window_close = 1'b0;
            @(posedge clk);
            #1;
        end
    endtask

    task confirmar_resultado;
        begin
            @(negedge clk);
            result_ack = 1'b1;
            @(posedge clk);
            #1;
            if (result_ready !== 1'b0 || eval_count !== 11'd0 ||
                votos_p0 !== 11'd0 || votos_p1 !== 11'd0 ||
                votos_p2 !== 11'd0 || votos_p3 !== 11'd0) begin
                $display("FALHOU: result_ack nao limpou contadores");
                erros = erros + 1;
            end
            @(negedge clk);
            result_ack = 1'b0;
        end
    endtask

    task conferir;
        input integer qtd;
        input integer p0_esp;
        input integer p2_esp;
        input [1:0] resultado_esp;
        input [2:0] motivo_esp;
        begin
            $display("Coleiras=%0d | votos P0=%0d P1=%0d P2=%0d P3=%0d | entradas=%0d | vencedor=P%0d motivo=%0d",
                     qtd,votos_p0,votos_p1,votos_p2,votos_p3,eval_count,recommended_profile,decision_reason);
            if (votos_p0 !== p0_esp || votos_p1 !== 11'd0 ||
                votos_p2 !== p2_esp || votos_p3 !== 11'd0 ||
                eval_count !== (qtd*5) || result_ready !== 1'b1 ||
                recommended_profile !== resultado_esp ||
                decision_reason !== motivo_esp || critical_alert !== 1'b0) begin
                $display("FALHOU: resultado esperado: P0=%0d P2=%0d total=%0d vencedor=P%0d motivo=%0d",
                         p0_esp,p2_esp,qtd*5,resultado_esp,motivo_esp);
                erros = erros + 1;
            end else begin
                $display("PASSOU: carga com %0d coleiras",qtd);
                casos_ok = casos_ok + 1;
            end
        end
    endtask

    // lim_p2 coleiras votam em P2, demais votam em P0.
    // Resultado esperado sempre P2 (lim_p2 > n/2).
    task carga;
        input integer n;
        input integer p2_collars;
        begin
            $display("=== CARGA: %0d coleiras x 5 minutos ===", n);
            for (minuto=0; minuto<5; minuto=minuto+1) begin
                for (idx=1; idx<=n; idx=idx+1) begin
                    if (idx<=p2_collars)
                        enviar(idx, minuto, 2);
                    else
                        enviar(idx, minuto, 0);
                    // Tentativa de duplicar o primeiro voto com dados opostos.
                    if (idx==1 && minuto==0)
                        enviar(8'd1,3'd0,0);
                end
            end
            // Entradas fora dos limites nao podem alterar o total.
            enviar(8'd0,  3'd0, 0);
            enviar(8'd255,3'd0, 0);
            enviar(8'd1,  3'd5, 0);
            fechar_janela();
            conferir(n,(n-p2_collars)*5,p2_collars*5,2'd2,3'b001);
            confirmar_resultado();
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
        casos_ok = 0;
        repeat (2) @(negedge clk);
        reset_n = 1'b1;

        carga(10,7);   // P2=35, P0=15, total 50
        carga(20,14);  // P2=70, P0=30, total 100
        carga(50,35);  // P2=175, P0=75, total 250
        carga(254,254);// P2=1270, P0=0: limite teorico de IDs

        $display("=== EMPATE: 20 coleiras x 5 minutos ===");
        for (minuto=0; minuto<5; minuto=minuto+1)
            for (idx=1; idx<=20; idx=idx+1)
                if (idx<=10) enviar(idx,minuto,2);
                else         enviar(idx,minuto,0);
        fechar_janela();
        conferir(20,50,50,2'd1,3'b101); // Empate: manter P1
        confirmar_resultado();

        $display("=== FIM DA SIMULACAO DE CARGA ===");
        $display("Casos aprovados=%0d/5; erros=%0d",casos_ok,erros);
        if (erros==0)
            $display("PASSOU: TODOS OS TESTES DE CARGA");
        else
            $display("FALHOU: TESTES DE CARGA");
        $finish;
    end
endmodule
