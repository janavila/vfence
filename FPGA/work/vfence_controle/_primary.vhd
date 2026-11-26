library verilog;
use verilog.vl_types.all;
entity vfence_controle is
    port(
        clk             : in     vl_logic;
        reset_n         : in     vl_logic;
        sample_valid    : in     vl_logic;
        window_close    : in     vl_logic;
        result_ack      : in     vl_logic;
        critical_next   : in     vl_logic;
        process_sample  : out    vl_logic;
        capture_normal  : out    vl_logic;
        capture_critical: out    vl_logic;
        clear_window    : out    vl_logic
    );
end vfence_controle;
