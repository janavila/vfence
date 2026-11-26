// VFence top: uma UO global, recebe UMA agregacao por pulso sample_valid.
// O Raspberry/Nios deve enviar ate 1 amostra por coleira por minuto.
// Registradores Avalon-MM, UART e comunicacao com Raspberry sao etapa posterior.
module vfence_top #(
    parameter integer MIN_VALID_VOTES = 3,
    parameter integer MIN_VOTING_MINUTES = 2
) (
    input  wire               clk,
    input  wire               reset_n,
    input  wire               sample_valid,
    input  wire               window_close,
    input  wire               result_ack,
    input  wire [1:0]         current_profile,
    input  wire [7:0]         collar_id,
    input  wire [2:0]         minute_index,
    input  wire [15:0]        pdr_x100,
    input  wire signed [15:0] snr_x100,
    input  wire [7:0]         rssi_mag,
    input  wire [15:0]        hdop_x100,
    input  wire [7:0]         sample_count,
    input  wire [3:0]         metrics_valid,
    output wire [1:0]         recommended_profile,
    output wire [2:0]         decision_reason,
    output wire               result_ready,
    output wire               critical_alert,
    output wire [8:0]         q_debug,
    output wire [1:0]         score_pdr_debug,
    output wire [1:0]         score_snr_debug,
    output wire [1:0]         score_rssi_debug,
    output wire [1:0]         score_hdop_debug,
    output wire [1:0]         vote_profile_debug,
    output wire               vote_valid_debug,
    output wire               duplicate_debug,
    output wire [10:0]        votos_p0,
    output wire [10:0]        votos_p1,
    output wire [10:0]        votos_p2,
    output wire [10:0]        votos_p3,
    output wire [10:0]        eval_count
);
    wire process_sample, capture_normal, capture_critical, clear_window;
    wire critical_next;

    vfence_controle u_controle (
        .clk(clk), .reset_n(reset_n), .sample_valid(sample_valid),
        .window_close(window_close), .result_ack(result_ack),
        .critical_next(critical_next), .process_sample(process_sample),
        .capture_normal(capture_normal), .capture_critical(capture_critical),
        .clear_window(clear_window)
    );
    vfence_operativa #(
        .MIN_VALID_VOTES(MIN_VALID_VOTES),
        .MIN_VOTING_MINUTES(MIN_VOTING_MINUTES)
    ) u_operativa (
        .clk(clk), .reset_n(reset_n), .current_profile(current_profile),
        .collar_id(collar_id), .minute_index(minute_index),
        .pdr_x100(pdr_x100), .snr_x100(snr_x100),
        .rssi_mag(rssi_mag), .hdop_x100(hdop_x100),
        .sample_count(sample_count), .metrics_valid(metrics_valid),
        .process_sample(process_sample), .capture_normal(capture_normal),
        .capture_critical(capture_critical), .clear_window(clear_window),
        .critical_next(critical_next),
        .score_pdr_debug(score_pdr_debug),
        .score_snr_debug(score_snr_debug),
        .score_rssi_debug(score_rssi_debug),
        .score_hdop_debug(score_hdop_debug),
        .q_debug(q_debug), .vote_profile_debug(vote_profile_debug),
        .vote_valid_debug(vote_valid_debug),
        .duplicate_debug(duplicate_debug),
        .votos_p0(votos_p0), .votos_p1(votos_p1),
        .votos_p2(votos_p2), .votos_p3(votos_p3),
        .eval_count(eval_count),
        .recommended_profile(recommended_profile),
        .decision_reason(decision_reason), .result_ready(result_ready),
        .critical_alert(critical_alert)
    );
endmodule
