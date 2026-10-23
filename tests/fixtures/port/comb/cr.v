// P11 시험용 원본: 클럭이 없는 조합 모듈(dec)과 리셋이 없는 파이프라인(pipe)
module dec (
    input  wire [1:0] sel,
    output reg  [3:0] onehot
);
    always @* begin
        onehot = 4'b0000;
        onehot[sel] = 1'b1;
    end
endmodule

module pipe (
    input  wire       clk,
    input  wire [3:0] d,
    output reg  [3:0] q
);
    reg [3:0] s;
    always @(posedge clk) begin
        s <= d;
        q <= s;
    end
endmodule

module cr_top (
    input  wire       clk,
    input  wire       rst,
    input  wire [1:0] sel,
    input  wire       en,
    output wire [3:0] q,
    output reg  [3:0] cnt
);
    wire [3:0] oh;
    dec  u_dec  (.sel(sel), .onehot(oh));
    pipe u_pipe (.clk(clk), .d(oh), .q(q));
    always @(posedge clk) begin
        if (rst) cnt <= 4'd0;
        else if (en) cnt <= cnt + 4'd1;
    end
endmodule
