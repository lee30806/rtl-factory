// P11 시험용 원본: 하위 모듈 안에서 push-pull로 구동하는 4비트 패드
module pp_core (
    input  wire       clk,
    input  wire       rst_n,
    inout  wire [3:0] pad,
    output reg  [3:0] q
);
    reg [3:0] dout;
    reg       drive;
    assign pad = drive ? dout : 4'bz;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dout  <= 4'd0;
            drive <= 1'b0;
            q     <= 4'd0;
        end else begin
            q     <= pad;
            drive <= ~drive;
            dout  <= dout + 4'd1;
        end
    end
endmodule

module pp_top (
    input  wire       clk,
    input  wire       rst_n,
    inout  wire [3:0] pad,
    output wire [3:0] q
);
    pp_core u_core (.clk(clk), .rst_n(rst_n), .pad(pad), .q(q));
endmodule
