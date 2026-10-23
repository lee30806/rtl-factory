// P12 실제 에이전트 시험용 원본 (2026-10-22): function을 쓰는 블록. fn_mod.ir.json과 같은 동작
// (task는 합성 RTL 사내 규칙으로 쓰지 않음)
module fn_top (
    input  wire       clk,
    input  wire       rst,
    input  wire [7:0] a,
    input  wire [7:0] b,
    output wire [7:0] y,
    output wire [7:0] z,
    output reg  [7:0] q
);
    function [7:0] mix(input [7:0] x, input [7:0] w);
        reg [7:0] t;
        begin
            t = x + w;
            mix = t ^ 8'h0F;
        end
    endfunction

    function [7:0] rot(input [7:0] x, input [2:0] k);
        reg [7:0] acc;
        integer i;
        begin
            acc = x;
            for (i = 0; i < 8; i = i + 1)
                if (i < k) acc = {acc[6:0], acc[7]};
            rot = mix(acc, 8'd1);
        end
    endfunction

    wire [7:0] ab = a ^ b;
    assign y = mix(a, b);
    assign z = {ab[7:4], ab[3:0] ^ ab[7:4]};
    always @(posedge clk) begin
        if (rst) q <= 8'd0;
        else     q <= rot(q ^ a, b[2:0]);
    end
endmodule
