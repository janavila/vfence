library verilog;
use verilog.vl_types.all;
entity vfence_operativa is
    port(
        clk             : in     vl_logic;
        reset_n         : in     vl_logic;
        current_profile : in     vl_logic_vector(1 downto 0);
        collar_id       : in     vl_logic_vector(7 downto 0);
        minute_index    : in     vl_logic_vector(2 downto 0);
        pdr_x100        : in     vl_logic_vector(15 downto 0);
        snr_x100        : in     vl_logic_vector(15 downto 0);
        rssi_mag        : in     vl_logic_vector(7 downto 0);
        hdop_x100       : in     vl_logic_vector(15 downto 0);
        sample_count    : in     vl_logic_vector(7 downto 0);
        metrics_valid   : in     vl_logic_vector(3 downto 0);
        process_sample  : in     vl_logic;
        capture_normal  : in     vl_logic;
        capture_critical: in     vl_logic;
        clear_window    : in     vl_logic;
        critical_next   : out    vl_logic;
        score_pdr_debug : out    vl_logic_vector(1 downto 0);
        score_snr_debug : out    vl_logic_vector(1 downto 0);
        score_rssi_debug: out    vl_logic_vector(1 downto 0);
        score_hdop_debug: out    vl_logic_vector(1 downto 0);
        q_debug         : out    vl_logic_vector(8 downto 0);
        vote_profile_debug: out    vl_logic_vector(1 downto 0);
        vote_valid_debug: out    vl_logic;
        duplicate_debug : out    vl_logic;
        votos_p0        : out    vl_logic_vector(10 downto 0);
        votos_p1        : out    vl_logic_vector(10 downto 0);
        votos_p2        : out    vl_logic_vector(10 downto 0);
        votos_p3        : out    vl_logic_vector(10 downto 0);
        eval_count      : out    vl_logic_vector(10 downto 0);
        recommended_profile: out    vl_logic_vector(1 downto 0);
        decision_reason : out    vl_logic_vector(2 downto 0);
        result_ready    : out    vl_logic;
        critical_alert  : out    vl_logic
    );
end vfence_operativa;
