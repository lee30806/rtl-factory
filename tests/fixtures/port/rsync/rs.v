// P11 시험용 원본: top 안의 2단 리셋 동기화기가 하위 모듈 리셋을 만듦
module rs_core (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] d,
    output reg  [7:0] q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= 8'd0;
        else        q <= q + d;
    end
endmodule

module rs_top (
    input  wire       clk,
    input  wire       arst_n,
    input  wire [7:0] d,
    output wire [7:0] q
);
    reg [1:0] sync;
    always @(posedge clk or negedge arst_n) begin
        if (!arst_n) sync <= 2'b00;
        else         sync <= {sync[0], 1'b1};
    end
    wire rst_n_s = sync[1];
    rs_core u_core (.clk(clk), .rst_n(rst_n_s), .d(d), .q(q));
endmodule
