// P11 시험용 Verilog-2001 원본: non-ANSI 포트, SV 예약어(logic)를 이름으로 씀, `ifdef, 헤더 include
module acc (clk, rst, logic, en, sum);
  input clk;
  input rst;
  input [`DW-1:0] logic;
  input en;
  output [`DW-1:0] sum;
  `include "params.vh"
  reg [`DW-1:0] sum;
  always @(posedge clk) begin
    if (rst)
      sum <= INIT;
    else if (en)
`ifdef USE_SUB
      sum <= sum - logic;
`else
      sum <= sum + logic + `STEP;
`endif
  end
endmodule
