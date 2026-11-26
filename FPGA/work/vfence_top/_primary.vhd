library verilog;
use verilog.vl_types.all;
entity vfence_top is
    port(
        clk             : in     vl_logic;
        reset_n         : in     vl_logic;
        sample_valid    : in     vl_logic;
        window_close    : in     vl_logic;
        result_ack      : in     vl_logic;
        current_profile : in     vl_logic_vector(1 downto 0);
        collar_id       : in     vl_logic_vector(7 downto 0);
        minute_index    : in     vl_logic_vector(2 downto 0);
        pdr_x100        : in     vl_logic_vector(15 downto 0);
        snr_x100        : in     vl_logic_vector(15 downto 0);
        rssi_mag        : in     vl_logic_vector(7 downto 0);
        hdop_x100       : in     vl_logic_vector(15 downto 0);
        sample_count    : in     vl_logic_vector(7 downto 0);
        metrics_valid   : in     vl_logic_vector(3 downto 0);
        recommended_profile: out    vl_logic_vector(1 downto 0);
        decision_reason : out    vl_logic_vector(2 downto 0);
        result_ready    : out    vl_logic;
        critical_alert  : out    vl_logic;
        q_debug         : out    vl_logic_vector(8 downto 0);
        score_pdr_debug : out    vl_logic_vector(1 downto 0);
        score_snr_debug : out    vl_logic_vector(1 downto 0);
        score_rssi_debug: out    vl_logic_vector(1 downto 0);
        score_hdop_debug: out    vl_logic_vector(1 downto 0);
        vote_profile_debug: out    vl_logic_vector(1 downto 0);
        vote_valid_debug: out    vl_logic;
        duplicate_debug : out    vl_logic;
        votos_p0        : out    vl_logic_vector(10 downto 0);
        votos_p1        : out    vl_logic_vector(10 downto 0);
        votos_p2        : out    vl_logic_vector(10 downto 0);
        votos_p3        : out    vl_logic_vector(10 downto 0);
        eval_count      : out    vl_logic_vector(10 downto 0)
    );
end vfence_top;
