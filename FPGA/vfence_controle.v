// VFence - Unidade de Controle (UC)
// Verilog-2001 / Quartus II 13.1
// Uma instancia do nucleo representa UMA coleira.
// A janela e encerrada por window_close (Raspberry/Nios), nao por clock FPGA.

module vfence_controle (
    input  wire clk,
    input  wire reset_n,
    input  wire sample_valid,
    input  wire window_close,
    input  wire result_ack,
    input  wire critical_next,

    output reg  process_sample,
    output reg  capture_normal,
    output reg  capture_critical,
    output reg  clear_window
);

    localparam [1:0] ST_COLLECT = 2'd0,
                     ST_NORMAL  = 2'd1,
                     ST_CRITICO = 2'd2,
                     ST_RESULT  = 2'd3;

    reg [1:0] state, next_state;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n)
            state <= ST_COLLECT;
        else
            state <= next_state;
    end

    always @(*) begin
        next_state       = state;
        process_sample   = 1'b0;
        capture_normal   = 1'b0;
        capture_critical = 1'b0;
        clear_window     = 1'b0;

        case (state)
            ST_COLLECT: begin
                if (sample_valid) begin
                    process_sample = 1'b1;
                    if (critical_next)
                        next_state = ST_CRITICO;
                    else if (window_close)
                        next_state = ST_NORMAL;
                end else if (window_close) begin
                    next_state = ST_NORMAL;
                end
            end
            ST_NORMAL: begin
                // Um ciclo depois do fechamento: os contadores ja foram atualizados.
                capture_normal = 1'b1;
                next_state = ST_RESULT;
            end
            ST_CRITICO: begin
                // O segundo PDR critico tem precedencia sobre o fechamento normal.
                capture_critical = 1'b1;
                next_state = ST_RESULT;
            end
            ST_RESULT: begin
                // Resultado fica estavel ate o software confirmar a LEITURA.
                if (result_ack) begin
                    clear_window = 1'b1;
                    next_state = ST_COLLECT;
                end
            end
            default: next_state = ST_COLLECT;
        endcase
    end
endmodule
