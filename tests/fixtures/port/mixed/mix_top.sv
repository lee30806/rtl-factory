// P11 시험용 SV top: package import, generate for/if, defparam, 상태 머신
module mix_top import mix_pkg::*; #(
  parameter int N = 2,
  parameter int MODE = 0
) (
  input  logic          clk,
  input  logic          rst,
  input  logic [`DW-1:0] a,
  input  logic          go,
  output logic [`DW-1:0] y0,
  output logic [`DW-1:0] y1,
  output logic [1:0]    state,
  output logic          busy,
  output logic [`DW-1:0] y2
);
  logic [`DW-1:0] s [N];
  st_t st;

  for (genvar i = 0; i < N; i++) begin : g_acc
    acc #(.INIT(i)) u_acc (clk, rst, a, go, s[i]);   // acc의 포트 이름 logic은 SV 예약어라 위치로 연결
  end
  // generate 배열 안 인스턴스의 defparam (Verilator 미지원이라 R1이 시뮬레이션 사본에서 인스턴스 파라미터로 옮김)
  defparam g_acc[1].u_acc.INIT = 6;
  logic [`DW-1:0] s_extra;
  acc u_extra (clk, rst, a, go, s_extra);
  defparam u_extra.INIT = 5;

  if (MODE == 0) begin : g_busy
    assign busy = (st != S_IDLE);
  end else begin : g_busy
    assign busy = 1'b0;
  end

  always_ff @(posedge clk) begin
    if (rst) st <= S_IDLE;
    else begin
      case (st)
        S_IDLE: if (go) st <= S_RUN;
        S_RUN:  if (s[0] >= K) st <= S_DONE;
        default: st <= S_IDLE;
      endcase
    end
  end

  assign state = st;
  assign y0 = s[0];
  assign y1 = s[N-1];
  assign y2 = s_extra;
endmodule
