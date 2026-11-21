// VFence - Unidade Operativa (UO)
// Verilog-2001 / Quartus II 13.1
// Classificador, pontuacao, voto, contadores, protecao critica, maioria e saida.
// UMA coleira por instancia. Nao ha memoria de estados para varias coleiras aqui.

module vfence_operativa (
    input  wire              clk,
    input  wire              reset_n,
    input  wire [1:0]        current_profile,
    input  wire [15:0]       pdr_x100,       // 0 a 10000 (100,00%)
    input  wire signed [15:0] snr_x100,     // dB x 100 (signed)
    input  wire [7:0]        rssi_mag,       // -115 dBm -> 115
    input  wire [7:0]        sample_count,    // telemetrias RX nesta agregacao
    input  wire [2:0]        metrics_valid,   // bit2=PDR; bit1=SNR; bit0=RSSI

    input  wire              process_sample,
    input  wire              capture_normal,
    input  wire              capture_critical,
    input  wire              clear_window,

    output wire              critical_next,
    output wire [1:0]        score_pdr_debug,
    output wire [1:0]        score_snr_debug,
    output wire [1:0]        score_rssi_debug,
    output wire [8:0]        q_debug,
    output wire [1:0]        vote_profile_debug,
    output wire              vote_valid_debug,
    output reg  [2:0]        votos_p0,
    output reg  [2:0]        votos_p1,
    output reg  [2:0]        votos_p2,
    output reg  [2:0]        votos_p3,
    output reg  [2:0]        eval_count,
    output reg  [1:0]        recommended_profile,
    output reg  [2:0]        decision_reason,
    output reg               result_ready,
    output reg               critical_alert
);
    // Codigos de motivo: 000=manter/sem maioria; 001=subir;
    // 010=descer; 011=critico; 100=janela incompleta.
    wire pdr_valid  = metrics_valid[2];
    wire snr_valid  = metrics_valid[1];
    wire rssi_valid = metrics_valid[0];

    // 1. Constantes por perfil. Referencias nominais (calibraveis futuramente).
    reg signed [15:0] snr_limit;
    reg [7:0] rssi_sensitivity;
    reg signed [15:0] snr_lower_limit;
    reg [7:0] rssi_lower_sensitivity;

    always @(*) begin
        case (current_profile)
            2'd0: begin
                snr_limit             = -16'sd1250;
                rssi_sensitivity      = 8'd129;
                snr_lower_limit       = -16'sd1250; // P0 nao pode descer
                rssi_lower_sensitivity= 8'd129;
            end
            2'd1: begin
                snr_limit             = -16'sd1500;
                rssi_sensitivity      = 8'd132;
                snr_lower_limit       = -16'sd1250; // candidato P0
                rssi_lower_sensitivity= 8'd129;
            end
            2'd2: begin
                snr_limit             = -16'sd1750;
                rssi_sensitivity      = 8'd133;
                snr_lower_limit       = -16'sd1500; // candidato P1
                rssi_lower_sensitivity= 8'd132;
            end
            default: begin
                snr_limit             = -16'sd2000;
                rssi_sensitivity      = 8'd136;
                snr_lower_limit       = -16'sd1750; // candidato P2
                rssi_lower_sensitivity= 8'd133;
            end
        endcase
    end

    // 2. Margens com sinal e extensao explicita: evitam underflow.
    wire signed [17:0] snr_ext = {{2{snr_x100[15]}}, snr_x100};
    wire signed [17:0] lim_ext = {{2{snr_limit[15]}}, snr_limit};
    wire signed [17:0] lim_lower_ext = {{2{snr_lower_limit[15]}}, snr_lower_limit};
    wire signed [17:0] snr_margin = snr_ext - lim_ext;
    wire signed [17:0] snr_lower_margin = snr_ext - lim_lower_ext;
    wire signed [9:0] rssi_margin = $signed({2'b00, rssi_sensitivity}) -
                                    $signed({2'b00, rssi_mag});
    wire signed [9:0] rssi_lower_margin = $signed({2'b00, rssi_lower_sensitivity}) -
                                          $signed({2'b00, rssi_mag});

    // 3. Scores discretos (0 a 3).
    wire [1:0] score_pdr = (pdr_x100 >= 16'd9500) ? 2'd3 :
                           (pdr_x100 >= 16'd8000) ? 2'd2 :
                           (pdr_x100 >= 16'd5000) ? 2'd1 : 2'd0;
    wire [1:0] score_snr = (snr_margin >= 18'sd600) ? 2'd3 :
                           (snr_margin >= 18'sd300) ? 2'd2 :
                           (snr_margin >= 18'sd0)   ? 2'd1 : 2'd0;
    wire [1:0] score_rssi = (rssi_margin >= 10'sd12) ? 2'd3 :
                            (rssi_margin >= 10'sd6)  ? 2'd2 :
                            (rssi_margin >= 10'sd0)  ? 2'd1 : 2'd0;

    assign score_pdr_debug  = score_pdr;
    assign score_snr_debug  = score_snr;
    assign score_rssi_debug = score_rssi;

    // 4. Pesos inteiros: Q = 50*P + 35*S + 15*R, Q entre 0 e 300.
    // Deslocamentos sao ligacoes de bits; nao exigem DSP.
    wire [8:0] p_ext = {7'd0, score_pdr};
    wire [8:0] s_ext = {7'd0, score_snr};
    wire [8:0] r_ext = {7'd0, score_rssi};
    wire [8:0] peso_p = (p_ext << 5) + (p_ext << 4) + (p_ext << 1);
    wire [8:0] peso_s = (s_ext << 5) + (s_ext << 1) + s_ext;
    wire [8:0] peso_r = (r_ext << 4) - r_ext;
    wire [8:0] q = peso_p + peso_s + peso_r;
    assign q_debug = q;

    // 5. Um voto normal somente se TODAS as metricas estiverem validas.
    // Uma agregacao com 0 pacotes ainda pode ter PDR valido (TX conhecido),
    // mas nao tem SNR/RSSI para gerar voto normal.
    wire voto_valido = pdr_valid && snr_valid && rssi_valid &&
                        (sample_count != 8'd0);
    wire aumentar = (score_pdr < 2'd2) || (q < 9'd175);
    wire reduzir  = (q >= 9'd260) && (score_pdr == 2'd3) &&
                    (current_profile != 2'd0) &&
                    (snr_lower_margin >= 18'sd600) &&
                    (rssi_lower_margin >= 10'sd12);

    wire [1:0] perfil_acima = (current_profile == 2'd3) ? 2'd3 :
                              (current_profile + 2'd1);
    wire [1:0] perfil_abaixo = (current_profile == 2'd0) ? 2'd0 :
                               (current_profile - 2'd1);
    wire [1:0] voto_perfil = aumentar ? perfil_acima :
                             reduzir  ? perfil_abaixo : current_profile;

    assign vote_profile_debug = voto_perfil;
    assign vote_valid_debug   = voto_valido;

    // 6. Ao receber a segunda avaliacao consecutiva valida com PDR < 50%,
    // a UC finaliza antes dos cinco minutos. Invalidas QUEBRAM a sequencia.
    reg [1:0] critical_streak;
    assign critical_next = (eval_count < 3'd5) && pdr_valid &&
                           (pdr_x100 < 16'd5000) && (critical_streak >= 2'd1);

    // 7. Atualizacao sequencial das contagens e dos resultados.
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            votos_p0 <= 3'd0;
            votos_p1 <= 3'd0;
            votos_p2 <= 3'd0;
            votos_p3 <= 3'd0;
            eval_count <= 3'd0;
            critical_streak <= 2'd0;
            recommended_profile <= 2'd0;
            decision_reason <= 3'd0;
            result_ready <= 1'b0;
            critical_alert <= 1'b0;
        end else if (clear_window) begin
            votos_p0 <= 3'd0;
            votos_p1 <= 3'd0;
            votos_p2 <= 3'd0;
            votos_p3 <= 3'd0;
            eval_count <= 3'd0;
            critical_streak <= 2'd0;
            result_ready <= 1'b0;
            critical_alert <= 1'b0;
            // recommended_profile e decision_reason ficam armazenados.
        end else begin
            if (process_sample && (eval_count < 3'd5)) begin
                // Uma avaliacao por minuto, mesmo se nao produzir voto.
                eval_count <= eval_count + 3'd1;
                if (voto_valido) begin
                    case (voto_perfil)
                        2'd0: votos_p0 <= votos_p0 + 3'd1;
                        2'd1: votos_p1 <= votos_p1 + 3'd1;
                        2'd2: votos_p2 <= votos_p2 + 3'd1;
                        default: votos_p3 <= votos_p3 + 3'd1;
                    endcase
                end
                if (pdr_valid && (pdr_x100 < 16'd5000)) begin
                    if (critical_streak < 2'd2)
                        critical_streak <= critical_streak + 2'd1;
                end else begin
                    critical_streak <= 2'd0;
                end
            end

            if (capture_critical) begin
                recommended_profile <= perfil_acima;
                decision_reason <= 3'b011; // critico
                critical_alert <= 1'b1;
                result_ready <= 1'b1;
            end else if (capture_normal) begin
                critical_alert <= 1'b0;
                result_ready <= 1'b1;
                if (eval_count != 3'd5) begin
                    recommended_profile <= current_profile;
                    decision_reason <= 3'b100; // fechamento antecipado/incompleto
                end else if (votos_p0 >= 3'd3) begin
                    recommended_profile <= 2'd0;
                    decision_reason <= (2'd0 < current_profile) ? 3'b010 : 3'b000;
                end else if (votos_p1 >= 3'd3) begin
                    recommended_profile <= 2'd1;
                    decision_reason <= (2'd1 > current_profile) ? 3'b001 :
                                       (2'd1 < current_profile) ? 3'b010 : 3'b000;
                end else if (votos_p2 >= 3'd3) begin
                    recommended_profile <= 2'd2;
                    decision_reason <= (2'd2 > current_profile) ? 3'b001 :
                                       (2'd2 < current_profile) ? 3'b010 : 3'b000;
                end else if (votos_p3 >= 3'd3) begin
                    recommended_profile <= 2'd3;
                    decision_reason <= (2'd3 > current_profile) ? 3'b001 : 3'b000;
                end else begin
                    recommended_profile <= current_profile;
                    decision_reason <= 3'b000; // manter/sem maioria
                end
            end
        end
    end
endmodule
