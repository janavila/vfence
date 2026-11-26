// VFence - Unidade Operativa (UO) - votacao global multicoleiras.
// Verilog-2001; indices 1..254, 5 minutos por janela.
// 1 voto elegivel por (collar_id, minuto). A ordem de chegada nao importa.
// Raspberry fornece dados agregados de 60 segundos e fechamento de 5 minutos.
// HDOP e qualidade GNSS, NAO mede qualidade LoRa; peso de 10% e experimental.
module vfence_operativa #(
    parameter integer MIN_VALID_VOTES = 3,
    parameter integer MIN_VOTING_MINUTES = 2
) (
    input  wire               clk,
    input  wire               reset_n,
    input  wire [1:0]         current_profile,
    input  wire [7:0]         collar_id,       // 1..254
    input  wire [2:0]         minute_index,    // 0..4 na janela atual
    input  wire [15:0]        pdr_x100,        // 0..10000
    input  wire signed [15:0] snr_x100,        // dB x 100 (signed)
    input  wire [7:0]         rssi_mag,        // -115 dBm => 115
    input  wire [15:0]        hdop_x100,       // HDOP x100, MENOR e melhor
    input  wire [7:0]         sample_count,    // pacotes RX na media RADIO
    input  wire [3:0]         metrics_valid,   // [3]=HDOP,[2]=PDR,[1]=SNR,[0]=RSSI
    input  wire               process_sample,
    input  wire               capture_normal,
    input  wire               capture_critical,
    input  wire               clear_window,
    output wire               critical_next,
    output wire [1:0]         score_pdr_debug,
    output wire [1:0]         score_snr_debug,
    output wire [1:0]         score_rssi_debug,
    output wire [1:0]         score_hdop_debug,
    output wire [8:0]         q_debug,
    output wire [1:0]         vote_profile_debug,
    output wire               vote_valid_debug,
    output wire               duplicate_debug,
    output reg  [10:0]        votos_p0,
    output reg  [10:0]        votos_p1,
    output reg  [10:0]        votos_p2,
    output reg  [10:0]        votos_p3,
    output reg  [10:0]        eval_count,
    output reg  [1:0]         recommended_profile,
    output reg  [2:0]         decision_reason,
    output reg                result_ready,
    output reg                critical_alert
);
    // Razoes: 000=manter, 001=subir, 010=descer, 011=critico,
    // 100=dados insuficientes, 101=empate. Critico apenas sinaliza alerta.
    wire hdop_valid = metrics_valid[3];
    wire pdr_valid  = metrics_valid[2];
    wire snr_valid  = metrics_valid[1];
    wire rssi_valid = metrics_valid[0];

    // Um bit por minuto, por coleira. Impede votos repetidos/retransmitidos
    // dentro da janela (sem precisar de 254 nucleos independentes).
    reg [4:0] seen_by_collar [0:255];
    reg [4:0] pdr_seen_by_collar [0:255];
    reg [4:0] low_pdr_by_collar [0:255];
    reg [4:0] voting_minutes;
    integer i;

    wire collar_ok = (collar_id >= 8'd1) && (collar_id <= 8'd254);
    wire minute_ok = (minute_index <= 3'd4);
    wire seen_before = collar_ok && minute_ok &&
                       seen_by_collar[collar_id][minute_index];
    wire accept_sample = process_sample && collar_ok && minute_ok && !seen_before;
    assign duplicate_debug = seen_before;
    wire accept_pdr = process_sample && collar_ok && minute_ok &&
                      pdr_valid && (pdr_x100 <= 16'd10000) &&
                      !pdr_seen_by_collar[collar_id][minute_index];
    wire [4:0] minute_bit = 5'b00001 << minute_index;

    // Limiares nominais historicos P0..P3 (calibraveis em campo).
    reg signed [15:0] snr_limit;
    reg [7:0] rssi_sensitivity;
    reg signed [15:0] snr_lower_limit;
    reg [7:0] rssi_lower_sensitivity;

    always @(*) begin
        case (current_profile)
            2'd0: begin
                snr_limit = -16'sd1250;
                rssi_sensitivity = 8'd129;
                snr_lower_limit = -16'sd1250;
                rssi_lower_sensitivity = 8'd129;
            end
            2'd1: begin
                snr_limit = -16'sd1500;
                rssi_sensitivity = 8'd132;
                snr_lower_limit = -16'sd1250;
                rssi_lower_sensitivity = 8'd129;
            end
            2'd2: begin
                snr_limit = -16'sd1750;
                rssi_sensitivity = 8'd133;
                snr_lower_limit = -16'sd1500;
                rssi_lower_sensitivity = 8'd132;
            end
            default: begin
                snr_limit = -16'sd2000;
                rssi_sensitivity = 8'd136;
                snr_lower_limit = -16'sd1750;
                rssi_lower_sensitivity = 8'd133;
            end
        endcase
    end

    wire signed [17:0] snr_ext = {{2{snr_x100[15]}}, snr_x100};
    wire signed [17:0] lim_ext = {{2{snr_limit[15]}}, snr_limit};
    wire signed [17:0] lower_ext = {{2{snr_lower_limit[15]}}, snr_lower_limit};
    wire signed [17:0] snr_margin = snr_ext - lim_ext;
    wire signed [17:0] snr_lower_margin = snr_ext - lower_ext;
    wire signed [9:0] rssi_margin = $signed({2'b00,rssi_sensitivity}) -
                                    $signed({2'b00,rssi_mag});
    wire signed [9:0] rssi_lower_margin = $signed({2'b00,rssi_lower_sensitivity}) -
                                          $signed({2'b00,rssi_mag});

    wire [1:0] score_pdr = (pdr_x100 >= 16'd9500) ? 2'd3 :
                           (pdr_x100 >= 16'd8000) ? 2'd2 :
                           (pdr_x100 >= 16'd5000) ? 2'd1 : 2'd0;
    wire [1:0] score_snr = (snr_margin >= 18'sd600) ? 2'd3 :
                           (snr_margin >= 18'sd300) ? 2'd2 :
                           (snr_margin >= 18'sd0)   ? 2'd1 : 2'd0;
    wire [1:0] score_rssi = (rssi_margin >= 10'sd12) ? 2'd3 :
                            (rssi_margin >= 10'sd6)  ? 2'd2 :
                            (rssi_margin >= 10'sd0)  ? 2'd1 : 2'd0;
    // Pontuacao HDOP proposta para simular; nao confundir com limite GNSS validado.
    wire [1:0] score_hdop = (hdop_x100 <= 16'd150) ? 2'd3 :
                            (hdop_x100 <= 16'd250) ? 2'd2 :
                            (hdop_x100 <= 16'd500) ? 2'd1 : 2'd0;

    assign score_pdr_debug  = score_pdr;
    assign score_snr_debug  = score_snr;
    assign score_rssi_debug = score_rssi;
    assign score_hdop_debug = score_hdop;

    // Equacao experimental de quatro metricas: maximo 300.
    // Q = 45*P + 30*S + 15*R + 10*H.
    wire [8:0] p_ext = {7'd0,score_pdr};
    wire [8:0] s_ext = {7'd0,score_snr};
    wire [8:0] r_ext = {7'd0,score_rssi};
    wire [8:0] h_ext = {7'd0,score_hdop};
    wire [8:0] peso_p = (p_ext << 5) + (p_ext << 3) + (p_ext << 2) + p_ext; // 45
    wire [8:0] peso_s = (s_ext << 5) - (s_ext << 1);                         // 30
    wire [8:0] peso_r = (r_ext << 4) - r_ext;                               // 15
    wire [8:0] peso_h = (h_ext << 3) + (h_ext << 1);                        // 10
    wire [8:0] q = peso_p + peso_s + peso_r + peso_h;
    assign q_debug = q;

    // Sem HDOP valido nao produz voto normal: nao transformar GNSS invalido
    // em avaliacao de qualidade ruim. O alerta critico de PDR e independente.
    wire voto_valido = hdop_valid && pdr_valid && snr_valid && rssi_valid &&
                        (sample_count != 8'd0) && (pdr_x100 <= 16'd10000) &&
                        (hdop_x100 != 16'hffff);

    // A salvaguarda de PDR baixo continua independente da equacao.
    wire aumentar = (score_pdr < 2'd2) || (q < 9'd175);
    wire reduzir = (q >= 9'd260) && (score_pdr == 2'd3) &&
                   (current_profile != 2'd0) &&
                   (snr_lower_margin >= 18'sd600) &&
                   (rssi_lower_margin >= 10'sd12);
    wire [1:0] perfil_acima = (current_profile == 2'd3) ? 2'd3 :
                              (current_profile + 2'd1);
    wire [1:0] perfil_abaixo = (current_profile == 2'd0) ? 2'd0 :
                               (current_profile - 2'd1);
    wire [1:0] voto_perfil = aumentar ? perfil_acima :
                             reduzir ? perfil_abaixo : current_profile;

    assign vote_profile_debug = voto_perfil;
    assign vote_valid_debug = accept_sample && voto_valido;

    // Critico: DUAS janelas consecutivas de PDR<50% DA MESMA COLEIRA.
    // Nao mistura duas coleiras nem dois resumos do mesmo minuto.
    wire [4:0] adjacent_minutes = (minute_bit << 1) | (minute_bit >> 1);
    assign critical_next = accept_pdr && (pdr_x100 < 16'd5000) &&
                           ((low_pdr_by_collar[collar_id] & adjacent_minutes) != 5'b0);

    // Vitoria por maior numero de votos; empate ou nenhum voto: manter.
    wire p0_wins = (votos_p0 > votos_p1) && (votos_p0 > votos_p2) && (votos_p0 > votos_p3);
    wire p1_wins = (votos_p1 > votos_p0) && (votos_p1 > votos_p2) && (votos_p1 > votos_p3);
    wire p2_wins = (votos_p2 > votos_p0) && (votos_p2 > votos_p1) && (votos_p2 > votos_p3);
    wire p3_wins = (votos_p3 > votos_p0) && (votos_p3 > votos_p1) && (votos_p3 > votos_p2);
    wire [1:0] winner = p0_wins ? 2'd0 : p1_wins ? 2'd1 :
                        p2_wins ? 2'd2 : p3_wins ? 2'd3 : current_profile;
    wire [2:0] voting_minute_count =
        {2'b0, voting_minutes[0]} + {2'b0, voting_minutes[1]} +
        {2'b0, voting_minutes[2]} + {2'b0, voting_minutes[3]} +
        {2'b0, voting_minutes[4]};
    wire sufficient = (eval_count >= MIN_VALID_VOTES) &&
                      (voting_minute_count >= MIN_VOTING_MINUTES);
    wire unique_winner = p0_wins || p1_wins || p2_wins || p3_wins;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            votos_p0 <= 11'd0;
            votos_p1 <= 11'd0;
            votos_p2 <= 11'd0;
            votos_p3 <= 11'd0;
            eval_count <= 11'd0;
            voting_minutes <= 5'b0;
            recommended_profile <= 2'd0;
            decision_reason <= 3'd0;
            result_ready <= 1'b0;
            critical_alert <= 1'b0;
            for (i=0; i<256; i=i+1) begin
                seen_by_collar[i] <= 5'b00000;
                pdr_seen_by_collar[i] <= 5'b0;
                low_pdr_by_collar[i] <= 5'b0;
            end
        end else if (clear_window) begin
            votos_p0 <= 11'd0;
            votos_p1 <= 11'd0;
            votos_p2 <= 11'd0;
            votos_p3 <= 11'd0;
            eval_count <= 11'd0;
            voting_minutes <= 5'b0;
            result_ready <= 1'b0;
            critical_alert <= 1'b0;
            // Recomendada e motivo continuam legiveis apos result_ack.
            for (i=0; i<256; i=i+1) begin
                seen_by_collar[i] <= 5'b00000;
                pdr_seen_by_collar[i] <= 5'b0;
                low_pdr_by_collar[i] <= 5'b0;
            end
        end else begin
            if (accept_pdr) begin
                pdr_seen_by_collar[collar_id][minute_index] <= 1'b1;
                low_pdr_by_collar[collar_id][minute_index] <= (pdr_x100 < 16'd5000);
            end
            if (accept_sample && voto_valido) begin
                seen_by_collar[collar_id][minute_index] <= 1'b1;
                eval_count <= eval_count + 11'd1;
                voting_minutes[minute_index] <= 1'b1;
                case (voto_perfil)
                    2'd0: votos_p0 <= votos_p0 + 11'd1;
                    2'd1: votos_p1 <= votos_p1 + 11'd1;
                    2'd2: votos_p2 <= votos_p2 + 11'd1;
                    default: votos_p3 <= votos_p3 + 11'd1;
                endcase
            end
            if (capture_critical) begin
                recommended_profile <= current_profile;
                decision_reason <= 3'b011;
                critical_alert <= 1'b1;
                result_ready <= 1'b1;
            end else if (capture_normal) begin
                recommended_profile <= sufficient ? winner : current_profile;
                critical_alert <= 1'b0;
                result_ready <= 1'b1;
                if (!sufficient)
                    decision_reason <= 3'b100;
                else if (!unique_winner)
                    decision_reason <= 3'b101;
                else if (winner > current_profile)
                    decision_reason <= 3'b001;
                else if (winner < current_profile)
                    decision_reason <= 3'b010;
                else
                    decision_reason <= 3'b000;
            end
        end
    end
endmodule
