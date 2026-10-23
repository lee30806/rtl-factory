// 하강 에지 도메인 시험용 원본: 같은 클럭의 상승 에지 단(보관)과 하강 에지 단(변환, 누적)
module ne_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       s_valid,
    output wire       s_ready,
    input  wire [7:0] s_data,
    output reg        m_valid,
    input  wire       m_ready,
    output wire [7:0] m_data,
    input  wire [3:0] n_in,
    output reg  [7:0] n_cnt,
    output reg  [7:0] p_q
);
    reg [7:0] hold;
    reg [7:0] conv;
    assign s_ready = !m_valid || m_ready;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            m_valid <= 1'b0;
            hold    <= 8'd0;
        end else if (s_ready) begin
            m_valid <= s_valid;
            hold    <= s_data;
        end
    end
    // 반주기 뒤 하강 에지에서 변환한다. 다음 상승 에지의 m_data는 hold ^ 8'h5A
    always @(negedge clk or negedge rst_n) begin
        if (!rst_n) conv <= 8'd0;
        else        conv <= hold ^ 8'h5A;
    end
    assign m_data = conv;
    // 하강 에지 단을 다음 상승 에지에서 받는다 (반주기 경로). 하강 에지 단이 상승 에지로 가면 한 사이클 늦어진다
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) p_q <= 8'd0;
        else        p_q <= conv;
    end
    always @(negedge clk or negedge rst_n) begin
        if (!rst_n) n_cnt <= 8'd0;
        else        n_cnt <= n_cnt + {4'd0, n_in};
    end
endmodule
