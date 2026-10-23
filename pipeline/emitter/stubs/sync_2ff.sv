// 임시 표준 동기화 셀. 사내 표준 셀 이름과 포트를 받으면 바꾼다 (P0 자료).
// 2단 플립플롭, active-low 리셋(0으로). ASYNC_RST=1이면 비동기 리셋, 0이면 동기 리셋 (설계의 리셋 방식을 따름).
// WIDTH > 1은 gray 코드 값에만 쓴다.
module sync_2ff #(
  parameter int WIDTH = 1,
  parameter bit ASYNC_RST = 1'b1
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [WIDTH-1:0] d,
  output logic [WIDTH-1:0] q
);
  logic [WIDTH-1:0] s1;
  if (ASYNC_RST) begin : g_async
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) begin
        s1 <= '0;
        q  <= '0;
      end else begin
        s1 <= d;
        q  <= s1;
      end
    end
  end else begin : g_sync
    always_ff @(posedge clk) begin
      if (!rst_n) begin
        s1 <= '0;
        q  <= '0;
      end else begin
        s1 <= d;
        q  <= s1;
      end
    end
  end
endmodule
